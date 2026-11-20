"""Local search index: a names file for fuzzy search and a deep text corpus.

names.tsv  table \t id \t scope_id \t scope \t label \t flags
deep.tsv   table \t id \t scope_id \t scope \t column \t text

Fuzzy search runs `fzf --filter` over names.tsv; deep search runs `rg` over
deep.tsv. Both files are rebuilt atomically from the read connection.

An environment with persist_index = false (PHI) keeps both in this
process's memory instead and feeds them to fzf and rg on stdin, so row
labels and text never reach the disk; the index then lives as long as the
server and is rebuilt on each start.
"""

import io
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from dex.config import INDEX_DIR, INDEX_MAX_AGE_SECONDS, environment, is_secret_column, settings
from dex.db import Database, quote_identifier
from dex.schema import Schema

# Label columns worth fuzzy-matching. Identifier-like labels (codes, paths,
# URLs) are left to exact lookup: as fuzzy candidates they only slow every
# keystroke and match any typed number.
NAME_LABEL_COLUMNS = {"name", "title", "username", "email", "label", "slug", "key", "value", "subject"}

# Deep search, when [search].deep_sources is not set: text columns whose
# names say people wrote them, ranked by this order (file order is rank, so
# comments and descriptions outrank generated text and paths).
DEEP_COLUMN_RANK = (
    "comment", "content", "body", "message", "note", "description", "summary", "bio",
    "caption", "subject", "title", "text", "prompt", "instructions", "url", "path",
)
DEEP_TEXT_TYPES = ("text", "json", "jsonb")
DEEP_MIN_VARCHAR = 120
DEEP_DEFAULT_CAP = 1500

CONTROL_CHARACTERS = re.compile(r"[\t\r\n\x00-\x1f]+")
FUZZY_RESULT_CAP = 60
DEEP_RESULT_CAP = 80
SEARCH_TIMEOUT_SECONDS = 3


@dataclass
class Hit:
    table: str
    id: str
    scope_id: str
    scope: str
    label: str
    source: str  # "id" | "fuzzy" | "deep"
    column: str = ""
    deleted: bool = False


def _flatten(value: object, cap: int) -> str:
    text = CONTROL_CHARACTERS.sub(" ", str(value)).strip()
    return text[:cap]


# One index per environment: staging rows must never answer a prod search.
def names_path(environment: str) -> str:
    return str(INDEX_DIR / environment / "names.tsv")


def deep_path(environment: str) -> str:
    return str(INDEX_DIR / environment / "deep.tsv")


@dataclass(frozen=True)
class _MemoryIndex:
    names: bytes
    deep: bytes
    built_at: float


# persist_index = false environments: environment name -> its index.
_memory: dict[str, _MemoryIndex] = {}
_memory_lock = threading.Lock()


def _persisted(environment_name: str) -> bool:
    return environment(environment_name).persist_index


def index_age_seconds(environment_name: str) -> float | None:
    if not _persisted(environment_name):
        with _memory_lock:
            built = _memory.get(environment_name)
        return None if built is None else time.time() - built.built_at
    try:
        return time.time() - os.path.getmtime(names_path(environment_name))
    except FileNotFoundError:
        return None


def index_is_stale(environment_name: str) -> bool:
    age = index_age_seconds(environment_name)
    if age is None or age > INDEX_MAX_AGE_SECONDS:
        return True
    return _persisted(environment_name) and not os.path.exists(deep_path(environment_name))


def scope_names(database: Database, schema: Schema) -> dict[str, str]:
    """scope id -> label, empty when no [scope] is configured or this
    environment's database has no such table."""
    scope = settings().scope
    if scope is None or scope.table not in schema.tables:
        return {}
    rows = database.read(f"SELECT id::text, {quote_identifier(scope.label)}::text FROM {schema.tables[scope.table].reference}",
                         row_cap=50_000).rows
    return {str(scope_id): "" if label is None else str(label) for scope_id, label in rows}


def _name_tables(schema: Schema) -> list[tuple[str, str]]:
    configured = settings()
    selected = []
    for name, table in sorted(schema.tables.items()):
        if name in configured.name_skip or not schema.has_id_key(name):
            continue
        if name in configured.label_overrides:
            selected.append((name, configured.label_overrides[name]))
        elif table.label_column in NAME_LABEL_COLUMNS:
            selected.append((name, f"{quote_identifier(table.label_column)}::text"))
    return selected


def _deep_rank(column: str) -> int | None:
    lowered = column.lower()
    for rank, fragment in enumerate(DEEP_COLUMN_RANK):
        if fragment in lowered:
            return rank
    return None


def deep_sources(schema: Schema) -> list[tuple[str, str, int]]:
    """(table, column, cap) to index for deep search: the config's list, or
    every long text column whose name marks it as written text."""
    configured = settings()
    if configured.deep_sources is not None:
        return list(configured.deep_sources)
    found = []
    for name, table in schema.tables.items():
        if name in configured.name_skip or not schema.has_id_key(name):
            continue
        for column in table.columns:
            column_type = table.column_types[column].lower()
            length = re.match(r"character varying\((\d+)\)", column_type)
            textual = column_type.startswith(DEEP_TEXT_TYPES) or (length and int(length.group(1)) >= DEEP_MIN_VARCHAR)
            rank = _deep_rank(column)
            if textual and rank is not None and not is_secret_column(column):
                found.append((rank, name, column))
    return [(name, column, DEEP_DEFAULT_CAP) for _, name, column in sorted(found)]


def build(database: Database, schema: Schema, progress: Callable[[str], None] = lambda message: None) -> dict:
    """Rebuild both files (or the in-memory pair). Each process writes its own
    temp files and renames them into place, so a CLI build and a server build
    never interleave."""
    environment_name = database.environment
    scopes = scope_names(database, schema)
    started = time.perf_counter()
    counts = {"names": 0, "deep": 0}
    names, deep = io.StringIO(), io.StringIO()

    for table_name, label_sql in _name_tables(schema):
        progress(f"names · {table_name}")
        table = schema.tables[table_name]
        sql = (
            f"SELECT id::text, {_scope_sql(table)}, {label_sql}, NOT ({table.live_condition()}) FROM {table.reference} "
            f"WHERE {label_sql} IS NOT NULL AND {label_sql} <> ''"
        )
        for row_id, scope_id, label, deleted in database.stream(sql):
            scope_key = "" if scope_id is None else str(scope_id)
            flags = "d" if deleted else ""
            names.write(f"{table_name}\t{row_id}\t{scope_key}\t{scopes.get(scope_key, '')}\t{_flatten(label, 300)}\t{flags}\n")
            counts["names"] += 1

    for table_name, column, cap in deep_sources(schema):
        table = schema.tables.get(table_name)
        if table is None or column not in table.columns:
            progress(f"deep · skipped {table_name}.{column} (not in schema)")
            continue
        progress(f"deep · {table_name}.{column}")
        text_sql = f"{quote_identifier(column)}::text"
        sql = (
            f"SELECT id::text, {_scope_sql(table)}, left({text_sql}, {int(cap)}) FROM {table.reference} "
            f"WHERE {quote_identifier(column)} IS NOT NULL AND {text_sql} <> ''"
        )
        for row_id, scope_id, text in database.stream(sql):
            scope_key = "" if scope_id is None else str(scope_id)
            deep.write(f"{table_name}\t{row_id}\t{scope_key}\t{scopes.get(scope_key, '')}\t{column}\t{_flatten(text, cap)}\n")
            counts["deep"] += 1

    if _persisted(environment_name):
        (INDEX_DIR / environment_name).mkdir(parents=True, exist_ok=True, mode=0o700)
        for path, content in ((names_path(environment_name), names), (deep_path(environment_name), deep)):
            temporary = f"{path}.{os.getpid()}.tmp"
            with open(temporary, "w") as output:
                output.write(content.getvalue())
            os.replace(temporary, path)
    else:
        built = _MemoryIndex(names.getvalue().encode(), deep.getvalue().encode(), time.time())
        with _memory_lock:
            _memory[environment_name] = built

    counts["seconds"] = round(time.perf_counter() - started, 1)
    return counts


def _scope_sql(table) -> str:
    return f"{quote_identifier(settings().scope.column)}::text" if table.has_scope else "NULL::text"


def _source(environment_name: str, kind: str) -> str | bytes | None:
    """The names or deep corpus: a file path, in-memory bytes, or None if unbuilt."""
    if _persisted(environment_name):
        path = names_path(environment_name) if kind == "names" else deep_path(environment_name)
        return path if os.path.exists(path) else None
    with _memory_lock:
        built = _memory.get(environment_name)
    if built is None:
        return None
    return built.names if kind == "names" else built.deep


def _feed(process: subprocess.Popen, content: bytes) -> threading.Thread:
    """Write content to process's stdin from a thread, so a capped reader can
    stop early without deadlocking on a full pipe."""
    def write() -> None:
        try:
            process.stdin.write(content)
            process.stdin.close()
        except (BrokenPipeError, ValueError):  # the reader stopped early and killed the process
            pass

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread


def _read_capped(command: list[str], cap: int, source: str | bytes) -> list[str]:
    in_memory = isinstance(source, bytes)
    stdin = subprocess.PIPE if in_memory else open(source, "rb")
    try:
        process = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if in_memory:
            _feed(process, source)
        lines = []
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line.decode(errors="replace").rstrip("\n"))
            if len(lines) >= cap or time.monotonic() > deadline:
                break
        process.kill()
        process.wait()
        return lines
    finally:
        if not in_memory:
            stdin.close()


def fuzzy(query: str, environment: str, cap: int = FUZZY_RESULT_CAP) -> list[Hit]:
    source = _source(environment, "names")
    if not query.strip() or source is None:
        return []
    # Match on table, scope, and label; ties go to the shorter, earlier match.
    # A bare number is an exact substring search: fuzzy-matching 22948
    # against scattered digits in names only buries the real hits.
    pattern = "'" + query.strip() if query.strip().isdigit() else query
    lines = _read_capped(
        ["fzf", "--filter", pattern, "--delimiter", "\t", "--nth", "1,4,5", "--tiebreak", "begin,length"],
        cap,
        source,
    )
    hits = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 6:
            table, row_id, scope_id, scope, label, flags = parts
            hits.append(Hit(table, row_id, scope_id, scope, label, "fuzzy", deleted="d" in flags))
    return hits


def deep(query: str, environment: str, cap: int = DEEP_RESULT_CAP) -> list[Hit]:
    """Every whitespace-separated term must appear (any order, smart case).

    One literal rg stage per term, longest first, each filtering the
    previous stage's output, so a common word ("slightly", 33k lines) can
    never crowd a rare match out of a capped read. Output keeps file order,
    which puts human-written sources (deep_sources order) first."""
    terms = sorted(set(query.split()), key=len, reverse=True)
    source = _source(environment, "deep")
    if not terms or source is None:
        return []
    case_flag = "--case-sensitive" if any(character.isupper() for character in query) else "--ignore-case"
    stages: list[subprocess.Popen] = []
    previous_output = None
    for position, term in enumerate(terms):
        command = ["rg", case_flag, "--fixed-strings", "--no-line-number", "--no-filename", "--", term]
        first_from_memory = position == 0 and isinstance(source, bytes)
        if position == 0 and not first_from_memory:
            command.append(source)
        stdin = subprocess.PIPE if first_from_memory else (previous_output or subprocess.DEVNULL)
        stage = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if first_from_memory:
            _feed(stage, source)
        if previous_output is not None:
            previous_output.close()  # the next stage owns the pipe now
        previous_output = stage.stdout
        stages.append(stage)
    hits = []
    deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
    try:
        for line in stages[-1].stdout:
            parts = line.decode(errors="replace").rstrip("\n").split("\t", 5)
            if len(parts) == 6:
                table, row_id, scope_id, scope, column, text = parts
                hits.append(Hit(table, row_id, scope_id, scope, _snippet(text, terms[0]), "deep", column=column))
            if len(hits) >= cap or time.monotonic() > deadline:
                break
    finally:
        for stage in stages:
            stage.kill()
            stage.wait()
    return hits


def _snippet(text: str, anchor: str, width: int = 140) -> str:
    position = text.lower().find(anchor.lower())
    if position < 0 or len(text) <= width:
        return text[:width]
    start = max(0, position - width // 3)
    prefix = "…" if start > 0 else ""
    return prefix + text[start:start + width]
