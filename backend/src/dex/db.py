"""Read access to one environment's Postgres database, plus result redaction.

Two transports sit behind one interface (read, stream, server_identity):

- PostgresDatabase connects with psycopg. Every read runs in its own
  `BEGIN READ ONLY` transaction that is rolled back afterwards, so a write
  that slipped past the SQL guard fails at the server, and a session setting
  changed by a statement never outlives it.
- SupabaseApiDatabase posts each read to Supabase's Management API query
  endpoint with read_only set, which Supabase runs as supabase_read_only_user
  in a read-only transaction. No database password exists on this machine.

Generated SQL uses %s placeholders (and %% for a literal percent sign) for
both; the API transport renumbers them to $1, $2, ….
"""

import datetime
import decimal
import json
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg

from dex import credentials
from dex.config import (
    READ_ROW_CAP,
    READ_STATEMENT_TIMEOUT_MS,
    REDACTED,
    Driver,
    Environment,
    environment,
    is_secret_column,
)

CONNECT_TIMEOUT_SECONDS = 6
# A single cell longer than this is cut for display; the UI can re-fetch the
# full row through inspect.
CELL_DISPLAY_LIMIT = 4000
# Concurrent reads: the card fans out up to PARALLEL_QUERIES (8) at once while
# searches and other requests run beside it.
POOL_SIZE = 12
BORROW_TIMEOUT_SECONDS = 30
KEEPALIVE_SECONDS = 240
# Index dumps read whole label columns; generous, but still bounded.
STREAM_STATEMENT_TIMEOUT_MS = 300_000

SUPABASE_QUERY_URL = "https://api.supabase.com/v1/projects/{ref}/database/query"
# Client-side bound on one API round trip. Supabase cancels a statement
# server-side at its own statement_timeout (2 min on this plan).
API_TIMEOUT_SECONDS = 30
# The Management API is rate limited per account; a card fans out ~6 reads.
API_CONCURRENCY = 4
# The API returns a whole result in one response; an index dump of a table
# larger than this is refused rather than silently cut.
API_STREAM_ROW_CAP = 200_000
# The row alias inside the JSON wrapper. A result column of the same name
# would shadow the row (Postgres resolves `json_agg(t)` to a column named t).
API_ROW_ALIAS = "__dex_row__"


class ReadOnlyViolation(Exception):
    """A read connection turned out to be able to write."""


class QueryError(Exception):
    """The database rejected or failed a statement; the message is the server's."""

    def __init__(self, message: str, sqlstate: str = ""):
        super().__init__(message)
        self.sqlstate = sqlstate


class ConnectionLost(Exception):
    """The database or API could not be reached, or dropped the request."""


@dataclass
class ReadResult:
    columns: list[str]
    rows: list[list[Any]]
    elapsed_ms: float
    truncated: bool = False
    redacted_columns: list[str] = field(default_factory=list)


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(text: str) -> str:
    # Valid only with standard_conforming_strings on (the default since 9.1);
    # server_identity reports it and Database.connect refuses a server without it.
    return "'" + text.replace("'", "''") + "'"


def to_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str, bool)):
        if isinstance(value, str) and len(value) > CELL_DISPLAY_LIMIT:
            return value[:CELL_DISPLAY_LIMIT] + f"… (+{len(value) - CELL_DISPLAY_LIMIT} chars)"
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime.datetime) else value.isoformat()
    if isinstance(value, (dict, list)):
        # jsonb and arrays: one readable cell, cut like any long text.
        return to_json_value(json.dumps(value, ensure_ascii=False, default=str))
    if isinstance(value, (datetime.timedelta, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    return str(value)


def redact(columns: list[str], rows: list) -> tuple[list[list[Any]], list[str]]:
    secret_positions = [index for index, column in enumerate(columns) if is_secret_column(column)]
    cleaned = []
    for row in rows:
        values = [to_json_value(value) for value in row]
        for index in secret_positions:
            if values[index] is not None:
                values[index] = REDACTED
        cleaned.append(values)
    return cleaned, [columns[index] for index in secret_positions]


IDENTITY_SQL = (
    "SELECT current_user AS user, current_database() AS database, "
    "current_setting('transaction_read_only') AS read_only, "
    "current_setting('standard_conforming_strings') AS standard_strings, "
    "current_setting('server_version') AS version"
)


class Database:
    """Read access to one environment; see the module docstring."""

    def __init__(self, environment_name: str):
        self.environment = environment_name
        self.target: Environment = environment(environment_name)

    def read(self, sql: str, parameters: tuple | None = None, row_cap: int = READ_ROW_CAP) -> ReadResult:
        """Run one already-guarded read statement."""
        started = time.perf_counter()
        columns, rows = self._fetch(sql, tuple(parameters or ()), row_cap + 1, READ_STATEMENT_TIMEOUT_MS)
        elapsed_ms = (time.perf_counter() - started) * 1000
        truncated = len(rows) > row_cap
        cleaned, redacted_columns = redact(columns, rows[:row_cap])
        return ReadResult(columns, cleaned, elapsed_ms, truncated, redacted_columns)

    def stream(self, sql: str):
        """Yield raw rows for an index dump."""
        raise NotImplementedError

    def server_identity(self) -> dict[str, Any]:
        """Who the reads run as, and proof they are read-only; refuses otherwise."""
        result = self.read(IDENTITY_SQL)
        identity = dict(zip(result.columns, result.rows[0]))
        if identity["read_only"] != "on":
            raise ReadOnlyViolation(f"{self.environment} reads are not in a read-only transaction; refusing to use them")
        if identity["standard_strings"] != "on":
            raise ReadOnlyViolation(f"{self.environment} has standard_conforming_strings off; dex's quoting needs it on")
        identity["transport"] = self.target.driver.value
        return identity

    def warm(self) -> None:
        """Pay connection setup before the first card needs it."""

    def keep_alive(self, stop: threading.Event, interval_seconds: float = KEEPALIVE_SECONDS) -> None:
        """Nothing to keep alive unless a transport holds connections."""

    def _fetch(self, sql: str, parameters: tuple, limit: int, timeout_ms: int) -> tuple[list[str], list]:
        raise NotImplementedError


def open_database(environment_name: str) -> Database:
    target = environment(environment_name)
    if target.driver is Driver.SUPABASE_API:
        return SupabaseApiDatabase(environment_name)
    return PostgresDatabase(environment_name)


# ── direct connection ─────────────────────────────────────────────────────
class PostgresDatabase(Database):
    """Pooled psycopg connections; every read is its own READ ONLY transaction."""

    def __init__(self, environment_name: str):
        super().__init__(environment_name)
        self._url: str | None = None
        self._url_lock = threading.Lock()
        self._idle: queue.LifoQueue[psycopg.Connection] = queue.LifoQueue()
        self._pool_lock = threading.Lock()
        self._open_count = 0

    def _get_url(self, refresh: bool = False) -> str:
        with self._url_lock:
            if self._url is None or refresh:
                self._url = credentials.read_secret(self.target)
            return self._url

    def _connect(self, refresh_credentials: bool = False) -> psycopg.Connection:
        # prepare_threshold=None: poolers in transaction mode (Neon's, PgBouncer)
        # cannot route a prepared statement back to the session that made it.
        connection = psycopg.connect(self._get_url(refresh=refresh_credentials), connect_timeout=CONNECT_TIMEOUT_SECONDS,
                                     prepare_threshold=None, autocommit=False)
        # psycopg opens every transaction as BEGIN READ ONLY.
        connection.read_only = True
        return connection

    def _open(self) -> psycopg.Connection:
        try:
            return self._connect()
        except psycopg.OperationalError as error:
            if "password authentication failed" not in str(error):
                raise ConnectionLost(f"{self.environment}: {str(error).strip()[:300]}") from None
            # The secret rotated since it was read.
            try:
                return self._connect(refresh_credentials=True)
            except psycopg.OperationalError as retry_error:
                raise ConnectionLost(f"{self.environment}: {str(retry_error).strip()[:300]}") from None

    def _borrow(self) -> psycopg.Connection:
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._pool_lock:
            can_open = self._open_count < POOL_SIZE
            if can_open:
                self._open_count += 1
        if can_open:
            try:
                return self._open()
            except BaseException:
                with self._pool_lock:
                    self._open_count -= 1
                raise
        try:
            return self._idle.get(timeout=BORROW_TIMEOUT_SECONDS)
        except queue.Empty:
            raise TimeoutError(f"all {POOL_SIZE} read connections busy for {BORROW_TIMEOUT_SECONDS}s") from None

    def _give_back(self, connection: psycopg.Connection, healthy: bool) -> None:
        if healthy and not connection.closed:
            self._idle.put(connection)
            return
        connection.close()
        with self._pool_lock:
            self._open_count -= 1

    def _discard_idle(self) -> None:
        while True:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                return
            self._give_back(connection, healthy=False)

    def _run(self, connection: psycopg.Connection, sql: str, parameters: tuple, limit: int, timeout_ms: int):
        try:
            with connection.cursor() as cursor:
                # SET LOCAL ends with the transaction, so a pooler that hands
                # this connection's server session to someone else leaks nothing.
                cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                cursor.execute("SET LOCAL TIME ZONE 'UTC'")
                cursor.execute(sql, parameters or None)
                columns = [column.name for column in cursor.description or []]
                rows = cursor.fetchmany(limit) if cursor.description else []
            return columns, rows
        finally:
            if not connection.closed:
                connection.rollback()

    def _fetch(self, sql: str, parameters: tuple, limit: int, timeout_ms: int) -> tuple[list[str], list]:
        for attempt in (1, 2):
            connection = self._borrow()
            healthy = True
            try:
                return self._run(connection, sql, parameters, limit, timeout_ms)
            except psycopg.Error as error:
                # A closed connection is the transport failing; an open one means
                # the server answered (psycopg files a cancelled statement under
                # OperationalError too, so the class alone cannot tell).
                healthy = not connection.closed
                if healthy:
                    raise QueryError(_server_message(error), error.sqlstate or "") from None
                if attempt == 1:
                    # A dropped server kills every pooled connection at once;
                    # retrying on another idle one would fail the same way.
                    self._discard_idle()
                    continue
                raise ConnectionLost(f"{self.environment}: {str(error).strip()[:300]}") from None
            finally:
                self._give_back(connection, healthy)
        raise AssertionError("unreachable")

    def stream(self, sql: str):
        connection = self._open()
        try:
            with connection.cursor(name="dex_index_dump") as cursor:
                connection.execute(f"SET LOCAL statement_timeout = {STREAM_STATEMENT_TIMEOUT_MS}")
                cursor.execute(sql)
                yield from cursor
        except psycopg.Error as error:
            raise QueryError(_server_message(error), error.sqlstate or "") from None
        finally:
            if not connection.closed:
                connection.rollback()
            connection.close()

    def warm(self) -> None:
        """Open a few connections in parallel: each costs a TLS handshake."""
        borrowed: list[psycopg.Connection] = []
        lock = threading.Lock()

        def open_one() -> None:
            connection = self._borrow()
            with lock:
                borrowed.append(connection)

        threads = [threading.Thread(target=open_one) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for connection in borrowed:
            self._give_back(connection, healthy=True)

    def keep_alive(self, stop: threading.Event, interval_seconds: float = KEEPALIVE_SECONDS) -> None:
        """Touch the pool periodically: servers and poolers drop idle sessions,
        which would make the next lookup pay a reconnect."""
        while not stop.wait(interval_seconds):
            try:
                self.read("SELECT 1")
            except (ConnectionLost, QueryError, TimeoutError):  # the next real read reconnects and reports
                pass


def _server_message(error: psycopg.Error) -> str:
    diagnostic = getattr(error, "diag", None)
    primary = diagnostic.message_primary if diagnostic else None
    return primary or str(error).strip().splitlines()[0]


# ── Supabase Management API ───────────────────────────────────────────────
def _numbered_placeholders(sql: str, count: int) -> str:
    """psycopg-style %s / %% to Postgres $1 … $n / %."""
    pieces, position, number = [], 0, 0
    while True:
        found = sql.find("%", position)
        if found < 0:
            pieces.append(sql[position:])
            break
        pieces.append(sql[position:found])
        marker = sql[found:found + 2]
        if marker == "%%":
            pieces.append("%")
        elif marker == "%s":
            number += 1
            pieces.append(f"${number}")
        else:
            raise ValueError(f"unescaped % at offset {found}; write %% for a literal percent sign")
        position = found + 2
    if number != count:
        raise ValueError(f"SQL has {number} placeholders but {count} parameters were given")
    return "".join(pieces)


def _json_parameter(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class SupabaseApiDatabase(Database):
    """Reads over HTTPS; no connection, no password, no pool to keep alive."""

    def __init__(self, environment_name: str):
        super().__init__(environment_name)
        self._token: str | None = None
        self._token_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(API_CONCURRENCY)
        self.url = SUPABASE_QUERY_URL.format(ref=self.target.project_ref)

    def _get_token(self, refresh: bool = False) -> str:
        with self._token_lock:
            if self._token is None or refresh:
                self._token = credentials.read_secret(self.target)
            return self._token

    def _wrap(self, sql: str, parameters: tuple, limit: int, timeout_ms: int) -> str:
        """One JSON text cell holding every row, in order, with every column.

        The API answers with JSON objects keyed by column name, which loses
        duplicate names (a.id, b.id) and the column order. A json text value
        keeps both. json_agg over a subquery aggregates in the subquery's
        output order (the Postgres docs' documented idiom for ordered input)."""
        if sql.lstrip()[:7].lower() == "explain":
            body = sql  # cannot be a subquery; one "QUERY PLAN" column, so nothing to lose
        else:
            body = (f"SELECT coalesce(json_agg({API_ROW_ALIAS}), '[]')::text AS dex_rows "
                    f"FROM (SELECT * FROM ({sql}) AS dex_query LIMIT {int(limit)}) AS {API_ROW_ALIAS}")
        if parameters:
            # The extended protocol (needed for parameters) takes one command,
            # so the SET LOCAL timeout cannot ride along. Parameterized SQL is
            # dex's own bounded queries; typed SQL never has parameters.
            return _numbered_placeholders(body, len(parameters))
        return f"SET LOCAL statement_timeout = {int(timeout_ms)}; {body}"

    def _post(self, query: str, parameters: tuple) -> list[dict]:
        payload: dict[str, Any] = {"query": query, "read_only": True}
        if parameters:
            payload["parameters"] = [_json_parameter(value) for value in parameters]
        for attempt in (1, 2):
            request = urllib.request.Request(
                self.url, data=json.dumps(payload).encode(), method="POST",
                headers={"Authorization": f"Bearer {self._get_token(refresh=attempt == 2)}",
                         "Content-Type": "application/json"},
            )
            try:
                with self._slots, urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
                    body = json.loads(response.read())
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")
                if error.code in (401, 403) and attempt == 1:
                    continue  # the token may have been rotated since it was read
                if error.code in (401, 403):
                    raise credentials.CredentialsError(f"{self.environment}: the Management API refused the token ({error.code})") from None
                if error.code == 429:
                    raise ConnectionLost(f"{self.environment}: Management API rate limit; wait a moment") from None
                if error.code == 400:
                    raise _api_query_error(detail) from None
                raise ConnectionLost(f"{self.environment}: Management API {error.code}: {detail[:300]}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                raise ConnectionLost(f"{self.environment}: {getattr(error, 'reason', error)}") from None
            if not isinstance(body, list):
                raise ConnectionLost(f"{self.environment}: unexpected Management API reply {str(body)[:200]}")
            return body
        raise AssertionError("unreachable")

    def _fetch(self, sql: str, parameters: tuple, limit: int, timeout_ms: int) -> tuple[list[str], list]:
        body = self._post(self._wrap(sql, parameters, limit, timeout_ms), parameters)
        if sql.lstrip()[:7].lower() == "explain":
            columns = list(body[0]) if body else ["QUERY PLAN"]
            return columns, [list(row.values()) for row in body]
        if len(body) != 1 or "dex_rows" not in body[0]:
            raise ConnectionLost(f"{self.environment}: the query reply had no dex_rows cell")
        objects = json.loads(body[0]["dex_rows"], object_pairs_hook=_Pairs)
        if not objects:
            # An empty result carries no column names over this transport.
            return [], []
        columns = [name for name, _ in objects[0]]
        return columns, [[_plain(value) for _, value in pairs] for pairs in objects]

    def stream(self, sql: str):
        _, rows = self._fetch(sql, (), API_STREAM_ROW_CAP + 1, STREAM_STATEMENT_TIMEOUT_MS)
        if len(rows) > API_STREAM_ROW_CAP:
            raise QueryError(f"index dump over {API_STREAM_ROW_CAP} rows is too large for the Management API transport")
        yield from rows


class _Pairs(list):
    """A JSON object kept as (key, value) pairs: rows keep duplicate column names."""


def _plain(value: Any) -> Any:
    """Nested JSON (a jsonb cell) back to ordinary dicts and lists."""
    if isinstance(value, _Pairs):
        return {key: _plain(item) for key, item in value}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _api_query_error(detail: str) -> QueryError:
    """'Failed to run sql query: ERROR:  42P01: relation "x" does not exist …'"""
    try:
        message = json.loads(detail).get("message", detail)
    except (json.JSONDecodeError, AttributeError):
        message = detail
    text = message.split("ERROR:", 1)[-1].strip()
    sqlstate, _, rest = text.partition(":")
    if len(sqlstate) == 5 and sqlstate.isalnum():
        return QueryError(rest.strip().splitlines()[0], sqlstate)
    return QueryError(text.splitlines()[0] if text else message[:300])
