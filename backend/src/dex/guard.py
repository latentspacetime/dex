"""Classify SQL as an allowed read, a write (always refused), or a rejection.

The guard fails closed: anything sqlglot cannot parse, any second statement,
and any statement kind not listed here is rejected. It is one layer of two;
every read also runs in a read-only transaction at the server.

A read-only transaction still lets a statement change session settings,
read server files, signal other backends, or run SQL the guard never saw
(query_to_xml takes a query string). Those functions are rejected here, and
only the data schema plus the system catalogs may be named: Supabase's auth,
storage and vault schemas and Neon's neon_auth hold credentials.
"""

import re

from dataclasses import dataclass
from enum import Enum

import sqlglot
from sqlglot import exp

from dex.config import READABLE_SCHEMAS, is_denied_table, is_secret_column

DIALECT = "postgres"

# Function names (lower-case) and prefixes that act outside the query's own
# rows: session state, the server's files, other backends, nested SQL text.
DENIED_FUNCTIONS = frozenset({
    "set_config", "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
    "pg_stat_file", "pg_import_system_collations", "pg_promote", "pg_switch_wal", "pg_create_restore_point",
    "dblink", "dblink_exec", "dblink_connect", "query_to_xml", "query_to_xml_and_xmlschema",
    "query_to_xmlschema", "cursor_to_xml", "cursor_to_xmlschema", "pg_notify",
})
DENIED_FUNCTION_PREFIXES = ("pg_read_", "pg_ls_", "lo_", "pg_advisory", "pg_try_advisory", "pg_file", "table_to_xml",
                            "schema_to_xml", "database_to_xml", "pg_logical_", "pg_replication_")
# EXPLAIN [ANALYZE] [VERBOSE] or EXPLAIN (options) ahead of the statement it explains.
EXPLAIN_PREFIX = re.compile(r"^explain(\s*\([^)]*\)|\s+analyze|\s+verbose)*\s+", re.IGNORECASE)


class StatementKind(Enum):
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    INSERT = "insert"


class GuardRejection(Exception):
    """The statement is not allowed; the message says why."""


@dataclass(frozen=True)
class GuardedStatement:
    kind: StatementKind
    sql: str
    tables: frozenset[str]
    expression: exp.Expression


def _strip(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _parse_single(sql: str) -> exp.Expression:
    try:
        statements = [statement for statement in sqlglot.parse(sql, read=DIALECT) if statement is not None]
    except sqlglot.errors.SqlglotError as error:  # ParseError and TokenError
        raise GuardRejection(f"could not parse SQL: {str(error).splitlines()[0]}") from None
    if len(statements) != 1:
        raise GuardRejection(f"exactly one statement is allowed, got {len(statements)}")
    return statements[0]


def _referenced_tables(expression: exp.Expression) -> frozenset[str]:
    cte_names = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    names = {table.name.lower() for table in expression.find_all(exp.Table) if table.name}
    return frozenset(names - cte_names)


def _function_name(function: exp.Func) -> str:
    return (function.name if isinstance(function, exp.Anonymous) else function.sql_name()).lower()


def _reject_sensitive_references(expression: exp.Expression, tables: frozenset[str]) -> None:
    denied = {table for table in tables if is_denied_table(table)}
    if denied:
        raise GuardRejection(f"table {sorted(denied)[0]} holds credentials and is never queried")
    for column in expression.find_all(exp.Column):
        if is_secret_column(column.name):
            raise GuardRejection(f"column {column.name} holds a secret and is never queried")
    for table in expression.find_all(exp.Table):
        if table.db and table.db.lower() not in READABLE_SCHEMAS:
            raise GuardRejection(f"schema {table.db} is outside what dex reads (public and the system catalogs)")
    for function in expression.find_all(exp.Func):
        name = _function_name(function)
        if name in DENIED_FUNCTIONS or name.startswith(DENIED_FUNCTION_PREFIXES):
            raise GuardRejection(f"function {name} acts outside the query and is not allowed")


def _classify(expression: exp.Expression) -> StatementKind:
    if isinstance(expression, (exp.Select, exp.SetOperation)):
        for select in expression.find_all(exp.Select):
            if select.args.get("locks"):
                raise GuardRejection("locking reads (FOR UPDATE / FOR SHARE) are not allowed")
        return StatementKind.READ
    if isinstance(expression, exp.Update):
        return StatementKind.UPDATE
    if isinstance(expression, exp.Delete):
        return StatementKind.DELETE
    if isinstance(expression, exp.Insert):
        return StatementKind.INSERT
    raise GuardRejection(f"{type(expression).__name__.upper()} statements are not allowed")


def inspect_statement(sql: str) -> GuardedStatement:
    cleaned = _strip(sql)
    if not cleaned:
        raise GuardRejection("empty statement")
    explained = EXPLAIN_PREFIX.match(cleaned)
    if explained:
        # sqlglot keeps EXPLAIN as an opaque command; guard what it explains.
        # EXPLAIN ANALYZE runs that statement, so it must be a read too.
        inner = inspect_statement(cleaned[explained.end():])
        if inner.kind is not StatementKind.READ:
            raise GuardRejection("EXPLAIN is allowed only for reads")
        return GuardedStatement(kind=StatementKind.READ, sql=cleaned, tables=inner.tables, expression=inner.expression)
    expression = _parse_single(cleaned)
    kind = _classify(expression)
    tables = _referenced_tables(expression)
    _reject_sensitive_references(expression, tables)
    return GuardedStatement(kind=kind, sql=cleaned, tables=tables, expression=expression)


def guard_read(sql: str, row_cap: int) -> str:
    """Return SQL safe to send to a read connection, bounded to row_cap + 1 rows."""
    statement = inspect_statement(sql)
    if statement.kind is not StatementKind.READ:
        raise GuardRejection(f"{statement.kind.value.upper()} is a write; dex is read-only on Postgres")
    expression = statement.expression
    if EXPLAIN_PREFIX.match(statement.sql):
        return statement.sql
    if isinstance(expression, (exp.Select, exp.SetOperation)) and not expression.args.get("limit"):
        # The extra row tells the caller the result was cut.
        return f"{statement.sql}\nLIMIT {row_cap + 1}"
    return statement.sql
