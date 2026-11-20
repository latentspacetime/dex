"""JSON-lines server for the nvim UI.

stdin:  {"id": 7, "op": "search", "args": {...}}
stdout: {"id": 7, "ok": true, "result": {...}}  |  {"id": 7, "ok": false, "error": {"kind": "...", "message": "..."}}
        {"event": "status", ...}                 (unsolicited progress)

Requests run concurrently; responses carry the request id, and the UI drops
any search response older than the latest one it sent.
"""

import json
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

import sqlglot
from sqlglot import exp

from dex import credentials, index, links, llm, router, schema
from dex import ask as ask_module
from dex.ask import DEFAULT_MODEL, MODEL_PROFILES, Asker
from dex.config import READ_ROW_CAP, ConfigError, environment, settings
from dex.db import ConnectionLost, Database, QueryError, ReadOnlyViolation, open_database
from dex.graph import Explorer
from dex.overview import OverviewBuilder
from dex.guard import DIALECT, GuardRejection, StatementKind, guard_read, inspect_statement

REQUEST_WORKERS = 8
SEARCH_WORKERS = 9
FUZZY_SHOWN = 40
DEEP_SHOWN = 30
SENTENCE_WORDS = 4


class WriteRefused(Exception):
    """dex is read-only on Postgres; every write operation answers with this."""


class LlmDisabled(Exception):
    """This environment keeps its data away from every LLM."""


READ_ONLY_MESSAGE = "dex is read-only on Postgres: nothing was written"


def classify_error(error: BaseException) -> str:
    if isinstance(error, credentials.AuthExpired):
        return "auth"
    # "tunnel" is the UI's word for "cannot reach the database".
    if isinstance(error, (credentials.CredentialsError, ConnectionLost, ReadOnlyViolation, ConfigError)):
        return "tunnel"
    if isinstance(error, (GuardRejection, LlmDisabled)):
        return "guard"
    if isinstance(error, WriteRefused):
        return "write"
    if isinstance(error, llm.LlmError):
        return "llm"
    if isinstance(error, (LookupError, KeyError)):
        return "not_found"
    if isinstance(error, QueryError):
        return "sql"
    return "internal"


def column_tables(columns: list[str], sql: str, known_tables: set[str]) -> dict[str, str]:
    """Which result columns hold an id of which table, so a row can be drilled.

    `id` (or `<name>_id` style names like customer_id) resolve to tables; `id`
    belongs to the first table in the outermost FROM."""
    mapping: dict[str, str] = {}
    main_table = None
    try:
        expression = sqlglot.parse_one(sql, read=DIALECT)
        source = expression.find(exp.From)
        table = source.find(exp.Table) if source else None
        main_table = table.name if table is not None else None
    except sqlglot.errors.SqlglotError:
        pass
    for column in columns:
        lowered = column.lower()
        if lowered == "id" and main_table in known_tables:
            mapping[column] = main_table
        elif lowered.endswith("_id"):
            stem = lowered[:-3]
            for candidate in (stem + "s", stem + "es", stem[:-1] + "ies" if stem.endswith("y") else stem, stem):
                if candidate in known_tables:
                    mapping[column] = candidate
                    break
    return mapping


def looks_like_identifier(query: str) -> bool:
    token = query.strip()
    return bool(token) and " " not in token and any(character.isdigit() for character in token)


class Server:
    def __init__(self, environment_name: str | None):
        # A missing or broken config is reported to the UI like any other
        # connection failure, rather than killing the server before it speaks.
        try:
            self.environment = environment(environment_name).name
            self.config_error: ConfigError | None = None
        except ConfigError as error:
            self.environment = environment_name or "unconfigured"
            self.config_error = error
        self.output_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(REQUEST_WORKERS, thread_name_prefix="dex-request")
        # Searches fan out from inside request workers; a separate pool means
        # a burst of searches can never starve its own sub-tasks.
        self.search_pool = ThreadPoolExecutor(SEARCH_WORKERS, thread_name_prefix="dex-search")
        self.ready = threading.Event()
        self.init_error: BaseException | None = None
        self.indexing = threading.Lock()
        self.database: Database | None = None
        self.schema: schema.Schema | None = None
        self.explorer: Explorer | None = None
        self.asker: Asker | None = None
        self.overview: OverviewBuilder | None = None
        self.keepalive_stop: threading.Event | None = None

    # ── output ───────────────────────────────────────────────────────────
    def emit(self, payload: dict) -> None:
        line = json.dumps(payload, default=str, ensure_ascii=False)
        with self.output_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def status(self, phase: str, message: str, **extra: Any) -> None:
        self.emit({"event": "status", "phase": phase, "message": message, **extra})

    # ── lifecycle ────────────────────────────────────────────────────────
    def initialize(self) -> None:
        self.ready.clear()
        self.init_error = None
        try:
            if self.config_error:
                raise self.config_error
            self.status("connect", f"connecting to {self.environment}")
            database = open_database(self.environment)
            identity = database.server_identity()
            self.status("schema", "reading schema")
            loaded = schema.load(database)
            scopes = index.scope_names(database, loaded)
            self.database, self.schema = database, loaded
            self.explorer = Explorer(database, loaded, scopes)
            # Asker reads value domains and builds the prompt; skipped entirely
            # where nothing may reach an LLM.
            self.asker = Asker(database, loaded, scopes) if database.target.llm_enabled else None
            self.overview = OverviewBuilder(database, loaded, self.explorer.pool, scopes)
            self.status("warm", "opening connections")
            database.warm()
            if self.keepalive_stop is not None:
                self.keepalive_stop.set()  # a re-login replaces the previous database
            self.keepalive_stop = threading.Event()
            threading.Thread(target=database.keep_alive, args=(self.keepalive_stop,), name="dex-keepalive", daemon=True).start()
            self.ready.set()
            self.status("ready", f"connected to {self.environment}", identity=identity, index_age=index.index_age_seconds(self.environment),
                        tables=len(loaded.tables), edges=len(loaded.edges))
            if index.index_is_stale(self.environment):
                self.pool.submit(self.reindex)
        except BaseException as error:  # reported to the UI, which offers the fix
            self.init_error = error
            self.status("error", str(error), kind=classify_error(error))

    def require_ready(self) -> None:
        if not self.ready.wait(timeout=60):
            raise self.init_error or RuntimeError("dex is still connecting")
        if self.init_error:
            raise self.init_error

    def reindex(self) -> dict:
        self.require_ready()
        if not self.indexing.acquire(blocking=False):
            return {"skipped": "an index build is already running"}
        try:
            self.status("index", "building search index")
            counts = index.build(self.database, self.schema, progress=lambda message: self.status("index", message))
            self.status("index_done", f"indexed {counts['names']} names, {counts['deep']} texts in {counts['seconds']}s", **counts)
            return counts
        finally:
            self.indexing.release()

    # ── operations ───────────────────────────────────────────────────────
    def op_hello(self, args: dict) -> dict:
        if self.config_error:
            return {"environment": self.environment, "environments": [self.environment], "protected": True,
                    "writes": False, "llm": False, "database": "", "can_login": False, "ready": False, "index_age": None,
                    "models": list(MODEL_PROFILES), "default_model": DEFAULT_MODEL}
        chosen = environment(self.environment)
        return {
            "environment": self.environment,
            "environments": list(settings().environments),
            "protected": chosen.protected,
            "writes": False,
            "llm": chosen.llm_enabled,
            "database": chosen.database,
            "can_login": credentials.can_login(self.environment),
            "ready": self.ready.is_set(),
            "index_age": index.index_age_seconds(self.environment),
            "models": list(MODEL_PROFILES),
            "default_model": DEFAULT_MODEL,
        }

    def op_search(self, args: dict) -> dict:
        """Local index only (fuzzy + deep): answers in tens of milliseconds and
        never waits on the database. Identifier lookup is op_lookup, which the
        UI sends alongside and merges when it lands."""
        query = str(args.get("q", ""))
        mode = args.get("mode", "auto")
        started = time.perf_counter()
        timings: dict[str, float] = {}

        def timed(name, function, *parameters):
            begun = time.perf_counter()
            try:
                return function(*parameters)
            finally:
                timings[name] = round((time.perf_counter() - begun) * 1000, 1)

        futures = {}
        if mode in ("auto", "fuzzy"):
            futures["fuzzy"] = self.search_pool.submit(timed, "fuzzy", index.fuzzy, query, self.environment)
        if mode in ("auto", "deep") and len(query.strip()) >= 3:
            futures["deep"] = self.search_pool.submit(timed, "deep", index.deep, query, self.environment)
        seen: set[tuple[str, str]] = set()
        hits = []
        # A sentence is almost never a name: fuzzy matching scatters its
        # letters across long labels, while a deep hit contains every word.
        order = (("deep", DEEP_SHOWN), ("fuzzy", FUZZY_SHOWN)) if len(query.split()) >= SENTENCE_WORDS \
            else (("fuzzy", FUZZY_SHOWN), ("deep", DEEP_SHOWN))
        for source, cap in order:
            if source not in futures:
                continue
            for hit in futures[source].result()[:cap]:
                key = (hit.table, hit.id)
                if key not in seen:
                    seen.add(key)
                    hits.append(asdict(hit))
        return {"q": query, "mode": mode, "hits": hits, "timings": timings,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}

    def op_lookup(self, args: dict) -> dict:
        """Exact matches: rows a URL names, then every table's id and
        external ids equal to the input."""
        query = str(args.get("q", ""))
        references = links.parse(query)
        if not references and not looks_like_identifier(query):
            return {"q": query, "hits": []}
        self.require_ready()
        started = time.perf_counter()
        if references:
            hits = [asdict(hit) for hit in self.explorer.resolve_references(references)]
        else:
            hits = [asdict(hit) for hit in self.explorer.lookup_identifier(query)]
        return {"q": query, "hits": hits, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}

    def _require_llm(self) -> None:
        if not environment(self.environment).llm_enabled:
            raise LlmDisabled(f"ASK is off for {self.environment}: its data must not leave this machine")

    def op_route(self, args: dict) -> dict:
        decided = router.route(str(args.get("q", "")), environment(self.environment).llm_enabled)
        return {"q": args.get("q", ""), **asdict(decided), "follow": decided.confidence >= router.FOLLOW_CONFIDENCE}

    def op_home(self, args: dict) -> dict:
        self.require_ready()
        return self.overview.build(refresh=bool(args.get("refresh")))

    def op_card(self, args: dict) -> dict:
        self.require_ready()
        return self.explorer.card(str(args["table"]), args["id"]).to_json()

    def op_ask(self, args: dict) -> dict:
        self._require_llm()
        self.require_ready()
        question = str(args["question"]).strip()
        model_key = args.get("model") or DEFAULT_MODEL
        if model_key not in MODEL_PROFILES:
            raise GuardRejection(f"unknown model {model_key}")
        result = self.asker.ask(question, model_key, force_query=bool(args.get("force")))
        payload = asdict(result)
        payload["column_tables"] = column_tables(result.columns, result.sql, set(self.schema.tables)) if result.sql else {}
        # Only a short-circuited lookup carries hits; a forced ask keeps the
        # router's verdict for display but answers with SQL.
        if result.route in ("search", "deep") and not result.sql and not result.error:
            payload["search"] = {"hits": self.op_lookup({"q": question})["hits"] + self.op_search({"q": question})["hits"]}
        return payload

    def op_summarize(self, args: dict) -> dict:
        """A plain-English answer from rows the UI already shows."""
        self._require_llm()
        completion = ask_module.summarize(
            str(args.get("question", "")), str(args.get("sql", "")), list(args.get("columns") or []),
            list(args.get("rows") or []), bool(args.get("truncated")), list(args.get("checks") or []),
        )
        return {"summary": completion.text.strip(), "model": completion.model,
                "elapsed_ms": round(completion.elapsed_ms), "cost_usd": completion.cost_usd}

    def op_sql(self, args: dict) -> dict:
        """SQL typed by the person: reads run, writes are refused."""
        self.require_ready()
        sql = str(args["sql"])
        statement = inspect_statement(sql)
        if statement.kind is not StatementKind.READ:
            raise WriteRefused(f"{statement.kind.value.upper()}: {READ_ONLY_MESSAGE}")
        result = self.database.read(guard_read(sql, READ_ROW_CAP))
        return {"columns": result.columns, "rows": result.rows, "truncated": result.truncated, "sql": sql,
                "column_tables": column_tables(result.columns, sql, set(self.schema.tables)),
                "elapsed_ms": round(result.elapsed_ms, 1), "redacted_columns": result.redacted_columns}

    # The UI's write affordances (e on a field, a typed UPDATE) land here.
    def op_edit_field(self, args: dict) -> dict:
        raise WriteRefused(READ_ONLY_MESSAGE)

    def op_plan_write(self, args: dict) -> dict:
        raise WriteRefused(READ_ONLY_MESSAGE)

    def op_execute_write(self, args: dict) -> dict:
        raise WriteRefused(READ_ONLY_MESSAGE)

    def op_discard_write(self, args: dict) -> dict:
        return {"discarded": True}  # no plan can exist, so there is nothing to discard

    def op_reindex(self, args: dict) -> dict:
        self.pool.submit(self.reindex)
        return {"started": True}

    def op_login(self, args: dict) -> dict:
        self.status("login", "running the login command; approve it if a browser opens")
        credentials.login(self.environment)
        self.initialize()
        if self.init_error:
            raise self.init_error
        return {"logged_in": True}

    def handle(self, request: dict) -> None:
        request_id = request.get("id")
        operation = getattr(self, f"op_{request.get('op')}", None)
        if operation is None:
            self.emit({"id": request_id, "ok": False, "error": {"kind": "internal", "message": f"unknown op {request.get('op')}"}})
            return
        try:
            result = operation(request.get("args") or {})
            self.emit({"id": request_id, "ok": True, "result": result})
        except BaseException as error:
            kind = classify_error(error)
            message = str(error) or type(error).__name__
            if kind == "internal":
                traceback.print_exc(file=sys.stderr)
            self.emit({"id": request_id, "ok": False, "error": {"kind": kind, "message": message}})

    def serve(self) -> None:
        threading.Thread(target=self.initialize, name="dex-init", daemon=True).start()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self.emit({"id": None, "ok": False, "error": {"kind": "internal", "message": "malformed request"}})
                continue
            self.pool.submit(self.handle, request)
