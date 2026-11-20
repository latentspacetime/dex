"""Natural language → one guarded SQL statement → result.

router.route runs beside SQL generation: a confident lookup (search or deep
verdict) skips the SQL answer, and a request to change data comes back as
drafted SQL with an error: dex is read-only on Postgres, so it never runs.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from dex import llm, router
from dex.config import ASK_LOG, READ_ROW_CAP, STATE_DIR, ConfigError, Settings, settings
from dex.db import Database, QueryError
from dex.guard import GuardRejection, StatementKind, guard_read, inspect_statement
from dex.emptycheck import check_filters
from dex.schema import Schema, prompt_schema, value_domains

# Facts any Postgres schema needs stated. Put your own schema traps and product
# vocabulary in [ask].context: accuracy comes mostly from such lines, each
# written after a wrong answer.
GENERAL_GOTCHAS = """\
- Soft deletes: a table with deleted_at means "not deleted" is t.deleted_at IS NULL; with is_deleted, NOT COALESCE(t.is_deleted, false).
- Double-quote identifiers that are reserved words or mixed case ("user", "order", "group", "createdAt").
- Text comparisons are case-sensitive; use ILIKE for case-insensitive matching.
- Columns typed text that hold dates compare as strings; cast them (col::date) before date arithmetic.
- Every table is in the public schema; never query other schemas.
- All timestamps are read in UTC. "In September 2026" means created_at >= '2026-09-01' AND created_at < '2026-10-01'."""

SYSTEM_TEMPLATE = """You write PostgreSQL queries for the {database} database.
Answer with exactly one SQL statement in a ```sql fenced block and nothing else.

Rules:
- Exactly one SELECT (WITH/CTEs allowed). Never write INSERT/UPDATE/DELETE/DDL, unless the user explicitly asks to change data; then write exactly one UPDATE/DELETE/INSERT on one table with a precise WHERE, which a human will review before anything runs.
- Return only the columns the question asks for, plus the identifying id or name when listing entities. Give columns readable aliases.
- For lists without an explicit size, LIMIT 100. Never select secret, password, or token columns.
- It is now {now} UTC. For relative windows ("last hour", "past 7 days", "today") use now() or current_date, e.g. created_at >= now() - interval '1 hour'; never subtract from a date literal.

Schema notes:
{gotchas}
{context}
Known values (exact, case-sensitive; every value that occurs in these columns):
{domains}
{scopes}
Schema (table (~rows): column type, →referenced table):
{schema}"""

SQL_BLOCK = re.compile(r"```(?:sql|postgres(?:ql)?)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
OPEN_FENCE = re.compile(r"```(?:sql|postgres(?:ql)?)?", re.IGNORECASE)
MAX_REPAIR_ATTEMPTS = 1
# The router misread 2 of 124 benchmark inputs, both noun-phrase questions;
# below this confidence a "lookup" verdict still gets an SQL answer.
LOOKUP_CONFIDENCE = 0.9
# Router verdicts that mean "find this record or text" rather than "answer".
LOOKUP_ROUTES = ("search", "deep")
ROUTE_TIMEOUT_SECONDS = 4

# OpenRouter model profiles. Ranked by an execution-graded benchmark
# (32 questions × 3 runs, 2026-10): gemini-3.8-flash@minimal 99% / p50 2.5 s;
# gpt-6-luna@minimal 99% / p50 2.8 s, 35× cheaper; muse-wonder@low 99%;
# inkling 92%; mercury-2.5 85%. Higher effort made Gemini worse. ^o in the UI
# cycles this order. [ask.models] in the config replaces the list.
DEFAULT_MODELS = {
    "gemini-3.8-flash": {"model": "google/gemini-3.8-flash", "reasoning": {"effort": "minimal"}},
    "gpt-6-luna": {"model": "openai/gpt-6-luna", "reasoning": {"effort": "minimal"}},
    "muse-wonder": {"model": "meta/muse-wonder", "reasoning": {"effort": "low"}},
    "inkling": {"model": "thinkingmachines/inkling", "reasoning": {"effort": "low"}},
    "mercury-2.5": {"model": "inception/mercury-2.5", "reasoning": {"effort": "low"}},
}


def _configured() -> Settings | None:
    """Settings, or None so `dex --help` and tests import without a config."""
    try:
        return settings()
    except ConfigError:
        return None


_CONFIGURED = _configured()


def _profiles() -> dict[str, llm.ModelProfile]:
    raw = (_CONFIGURED and _CONFIGURED.models) or DEFAULT_MODELS
    return {key: llm.ModelProfile(key, body["model"], {name: value for name, value in body.items() if name != "model"})
            for key, body in raw.items()}


MODEL_PROFILES = _profiles()
DEFAULT_MODEL = (_CONFIGURED and _CONFIGURED.default_model) or next(iter(MODEL_PROFILES))
# Summaries read rows, not schema: a cheap fast model is plenty.
DEFAULT_SUMMARY_MODEL = (_CONFIGURED and _CONFIGURED.summary_model) or (
    "gpt-6-luna" if "gpt-6-luna" in MODEL_PROFILES else DEFAULT_MODEL)


@dataclass
class AskResult:
    question: str
    route: str
    route_confidence: float
    model: str = ""
    sql: str = ""
    kind: str = "read"
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    error: str = ""
    attempts: int = 0
    llm_ms: float = 0.0
    route_ms: float = 0.0
    sql_ms: float = 0.0
    cost_usd: float = 0.0
    # Lines from emptycheck when an empty result was checked (and retried).
    checks: list[str] = field(default_factory=list)


# Scope rows listed in the prompt so names resolve exactly; more than this
# and the list costs more tokens than it saves.
PROMPT_SCOPE_LIMIT = 2000


def build_system_prompt(database_name: str, schema: Schema, scopes: dict[str, str], domains: dict[str, list[str]]) -> str:
    domain_lines = "\n".join(f"{column}: {', '.join(values)}" for column, values in domains.items())
    scope_text = ""
    scope = settings().scope
    if scope and scopes and len(scopes) <= PROMPT_SCOPE_LIMIT:
        listed = "\n".join(f"{scope_id}: {name}" for scope_id, name in scopes.items())
        scope_text = (f"\n{scope.table} (resolve these by exact name; other tables reference them by "
                      f"{scope.column}) as id: {scope.label}:\n{listed}\n")
    context = settings().ask_context
    return SYSTEM_TEMPLATE.format(
        database=database_name,
        now=time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        gotchas=GENERAL_GOTCHAS,
        context=f"{context}\n" if context else "",
        domains=domain_lines or "(none detected)",
        scopes=scope_text,
        schema=prompt_schema(schema),
    )


SUMMARY_PROMPT = """You summarize a database query result for the engineer who asked the question.
Write 2-4 plain sentences that answer the question directly from the rows: lead with the answer, then the notable
specifics (names, counts, dates, outliers). Use only what the rows show; never guess beyond them. If the result was
cut at a row cap, say the summary covers the rows shown. If there are no rows, say what that means for the question,
using the filter checks if given. No markdown, no preamble."""
SUMMARY_ROWS = 60
SUMMARY_CELL_CHARS = 200


def summarize(question: str, sql: str, columns: list[str], rows: list[list[Any]], truncated: bool,
              checks: list[str], model_key: str = DEFAULT_SUMMARY_MODEL) -> llm.Completion:
    shown = rows[:SUMMARY_ROWS]
    table = "\n".join(
        "\t".join("NULL" if value is None else str(value)[:SUMMARY_CELL_CHARS] for value in row) for row in shown
    )
    note = []
    if truncated or len(rows) > SUMMARY_ROWS:
        note.append(f"(showing {len(shown)} of {'more than ' if truncated else ''}{len(rows)} rows)")
    if checks:
        note.append("Filter checks: " + " ".join(checks))
    content = (f"Question: {question}\n\nSQL:\n{sql}\n\nColumns: {chr(9).join(columns)}\n"
               f"Rows ({len(rows)}):\n{table or '(none)'}\n{' '.join(note)}")
    return llm.chat(MODEL_PROFILES[model_key], [{"role": "system", "content": SUMMARY_PROMPT},
                                                 {"role": "user", "content": content}], max_tokens=400)


def extract_sql(text: str) -> str:
    blocks = SQL_BLOCK.findall(text)
    if blocks:
        return blocks[-1].strip()
    # An opening fence whose close was cut or mangled (one model ends with "``").
    opening = OPEN_FENCE.search(text)
    stripped = text[opening.end():] if opening else text
    stripped = stripped.strip().rstrip("`").strip()
    if re.match(r"^(select|with|explain|update|delete|insert)\b", stripped, re.IGNORECASE):
        return stripped
    raise llm.LlmError(f"no SQL in model reply: {text[:200]!r}")


class Asker:
    def __init__(self, database: Database, schema: Schema, scopes: dict[str, str]):
        self.database = database
        self.schema = schema
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dex-ask")
        self.domains = value_domains(database, schema)
        self.system_prompt = build_system_prompt(database.target.database, schema, scopes, self.domains)

    def generate(self, question: str, model_key: str, feedback: tuple[str, str] | None = None) -> llm.Completion:
        """feedback: (previous SQL, what went wrong with it) for a repair round."""
        messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": question}]
        if feedback:
            previous_sql, problem = feedback
            messages += [
                {"role": "assistant", "content": f"```sql\n{previous_sql}\n```"},
                {"role": "user", "content": problem},
            ]
        return llm.chat(MODEL_PROFILES[model_key], messages)

    def answer(self, question: str, model_key: str = DEFAULT_MODEL) -> AskResult:
        """Generate SQL, guard it, run it. One repair round for an error, and
        one for an empty result whose filters name values that do not exist."""
        result = AskResult(question=question, route="query", route_confidence=0.0, model=model_key)
        feedback = None
        repairs_left, empty_check_done = MAX_REPAIR_ATTEMPTS, False
        while True:
            result.attempts += 1
            try:
                completion = self.generate(question, model_key, feedback)
            except llm.LlmError as error:
                result.error = str(error)
                break
            result.llm_ms += completion.elapsed_ms
            result.cost_usd += completion.cost_usd
            try:
                result.sql = extract_sql(completion.text)
                statement = inspect_statement(result.sql)
            except (llm.LlmError, GuardRejection) as error:
                result.error = str(error)
                if repairs_left == 0:
                    break
                repairs_left -= 1
                feedback = (result.sql or completion.text[:500], f"That query failed: {error}\nReturn a corrected query.")
                continue
            if statement.kind is not StatementKind.READ:
                # Drafted write: shown, never executed; dex is read-only on Postgres.
                result.kind = statement.kind.value
                result.error = f"the model drafted {statement.kind.value.upper()}; dex is read-only on Postgres, so it did not run"
                break
            try:
                read = self.database.read(guard_read(result.sql, READ_ROW_CAP))
            except QueryError as error:  # the server's own message, for the repair round
                result.error = f"{error.sqlstate or 'error'}: {error}"
                if repairs_left == 0:
                    break
                repairs_left -= 1
                feedback = (result.sql, f"That query failed: {result.error}\nReturn a corrected query.")
                continue
            result.columns, result.rows, result.truncated = read.columns, read.rows, read.truncated
            result.sql_ms, result.error = read.elapsed_ms, ""
            if read.rows or empty_check_done:
                break
            empty_check_done = True
            check = check_filters(result.sql, self.database, self.domains)
            result.checks = check.lines
            if not check.found_problem:
                break  # every filter value exists: the empty answer stands
            feedback = (result.sql, "That query returned 0 rows. Its filters, checked against the data:\n"
                        + "\n".join(f"- {line}" for line in check.lines)
                        + "\nRewrite the query with values that exist. If 0 rows is still the right answer, return the same query.")
        _log(result)
        return result

    def ask(self, question: str, model_key: str = DEFAULT_MODEL, force_query: bool = False) -> AskResult:
        """Route and answer concurrently, so the router costs no wall time.

        A confident lookup returns at once (the caller shows search hits) and
        the SQL being generated is discarded; force_query skips that."""
        route_future = self.pool.submit(router.route, question, True) if settings().router_enabled else None
        answer_future = self.pool.submit(self.answer, question, model_key)
        decision = None
        try:
            decision = route_future.result(timeout=ROUTE_TIMEOUT_SECONDS) if route_future else None
        except Exception:
            # Routing is an optimization; without it every input is a query.
            pass
        if decision and decision.mode in LOOKUP_ROUTES and decision.confidence >= LOOKUP_CONFIDENCE and not force_query:
            return AskResult(question=question, route=decision.mode, route_confidence=decision.confidence,
                             model=model_key, route_ms=decision.elapsed_ms)
        result = answer_future.result()
        if decision:
            result.route, result.route_confidence, result.route_ms = decision.mode, decision.confidence, decision.elapsed_ms
        return result


def _log(result: AskResult) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "question": result.question, "model": result.model, "sql": result.sql,
        "rows": len(result.rows), "error": result.error, "llm_ms": round(result.llm_ms),
        "attempts": result.attempts, "cost_usd": result.cost_usd,
    }
    with open(ASK_LOG, "a") as log:
        log.write(json.dumps(entry) + "\n")
