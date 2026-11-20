"""Live schema introspection and the relationship graph built from it.

Edges come from four sources, in order of trust:
1. declared single-column foreign keys to a parent's id (pg_constraint),
2. undeclared `*_id` columns listed in the config ([schema.references]),
3. undeclared `<name>_id` columns whose name matches a table (`<name>s`,
   `<name>es`, `<name>`), unless schema.infer_references is false,
4. polymorphic (type, id) pairs from the config ([[schema.polymorphic]]).
Tables whose only purpose is joining two others (two FK columns, nothing
else of substance) become many-to-many edges that skip the join row.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from dex.config import DATA_SCHEMA, STATE_DIR, is_denied_table, is_secret_column, settings
from dex.db import Database, quote_identifier, quote_literal

SCHEMA_CACHE_MAX_AGE_SECONDS = 24 * 3600
SCHEMA_CACHE_VERSION = 6

# Columns that carry no meaning of their own on a join table.
JOIN_TABLE_INCIDENTAL_COLUMNS = {"id", "created_at", "updated_at", "position", "sort_order", "is_deleted", "deleted_at"}
# Soft-delete markers, in the order they are trusted.
SOFT_DELETE_COLUMNS = ("deleted_at", "is_deleted")
INTEGER_TYPES = ("smallint", "integer", "bigint")
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


@dataclass
class Edge:
    """child.column references parent.id (or the polymorphic equivalent)."""

    child: str
    column: str
    parent: str
    kind: str  # "fk" | "implicit" | "polymorphic"
    type_column: str | None = None
    type_value: str | None = None


@dataclass
class Table:
    name: str
    columns: list[str]
    column_types: dict[str, str]
    primary_key: list[str]
    estimated_rows: int
    indexed_leading_columns: list[str]
    label_column: str | None = None
    has_scope: bool = False
    soft_delete_column: str | None = None

    @property
    def reference(self) -> str:
        """The table as generated SQL names it: schema-qualified and quoted."""
        return f"{quote_identifier(DATA_SCHEMA)}.{quote_identifier(self.name)}"

    def parameter(self, column: str) -> str:
        """A placeholder cast to the column's type. Ids travel as text (the UI
        and JSON carry them as strings) and Postgres compares strictly, so
        `patient_id = 42` against a text column would be an error."""
        return f"%s::{self.column_types[column]}"

    def accepts(self, column: str, token: str) -> bool:
        """Whether token can be a value of column at all; others are skipped
        rather than sent, since a failed cast fails the whole UNION."""
        column_type = self.column_types[column]
        if column_type in INTEGER_TYPES or column_type.startswith("numeric"):
            return token.isdigit() and int(token) < 2**63
        if column_type == "uuid":
            return bool(UUID_PATTERN.match(token))
        return column_type == "text" or column_type.startswith(("character varying", "character("))

    def live_condition(self, alias: str = "") -> str:
        """SQL true for rows that are not soft-deleted ("TRUE" without a marker)."""
        prefix = f"{alias}." if alias else ""
        if self.soft_delete_column == "deleted_at":
            return f"{prefix}deleted_at IS NULL"
        if self.soft_delete_column == "is_deleted":
            return f"NOT COALESCE({prefix}is_deleted::boolean, false)"
        return "TRUE"


@dataclass
class Schema:
    tables: dict[str, Table]
    edges: list[Edge]
    join_tables: dict[str, tuple[Edge, Edge]] = field(default_factory=dict)
    built_at: float = 0.0

    def outgoing(self, table: str) -> list[Edge]:
        return [edge for edge in self.edges if edge.child == table]

    def incoming(self, table: str) -> list[Edge]:
        return [edge for edge in self.edges if edge.parent == table]

    def has_id_key(self, table: str) -> bool:
        return self.tables[table].primary_key == ["id"]

    def to_json(self) -> dict:
        return {
            "version": SCHEMA_CACHE_VERSION,
            "built_at": self.built_at,
            "tables": {name: asdict(table) for name, table in self.tables.items()},
            "edges": [asdict(edge) for edge in self.edges],
            "join_tables": {name: [asdict(pair[0]), asdict(pair[1])] for name, pair in self.join_tables.items()},
        }

    @classmethod
    def from_json(cls, payload: dict) -> "Schema":
        return cls(
            tables={name: Table(**table) for name, table in payload["tables"].items()},
            edges=[Edge(**edge) for edge in payload["edges"]],
            join_tables={name: (Edge(**pair[0]), Edge(**pair[1])) for name, pair in payload["join_tables"].items()},
            built_at=payload["built_at"],
        )


def _choose_label(columns: list[str]) -> str | None:
    present = set(columns)
    for candidate in settings().label_columns:
        if candidate in present and not is_secret_column(candidate):
            return candidate
    return None


def _detect_join_tables(tables: dict[str, Table], edges: list[Edge]) -> dict[str, tuple[Edge, Edge]]:
    scope = settings().scope
    incidental = JOIN_TABLE_INCIDENTAL_COLUMNS | ({scope.column} if scope else set())
    join_tables = {}
    for name, table in tables.items():
        table_edges = [edge for edge in edges if edge.child == name and edge.kind in ("fk", "implicit")]
        substantive = [column for column in table.columns if column not in incidental]
        if len(table_edges) == 2 and sorted(substantive) == sorted(edge.column for edge in table_edges):
            join_tables[name] = (table_edges[0], table_edges[1])
    return join_tables


def introspect(database: Database) -> Schema:
    namespace = quote_literal(DATA_SCHEMA)
    column_rows = database.read(
        "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
        "COALESCE(a.attnum = ANY(i.indkey), false) "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary "
        f"WHERE n.nspname = {namespace} AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY c.relname, a.attnum",
        row_cap=100_000,
    ).rows
    # reltuples is -1 for a table never vacuumed or analyzed: unknown, read as 0.
    table_rows = database.read(
        "SELECT c.relname, GREATEST(c.reltuples, 0)::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = {namespace} AND c.relkind IN ('r', 'p')",
        row_cap=10_000,
    ).rows
    # Edges point at a parent's id, so only single-column keys qualify.
    foreign_key_rows = database.read(
        "SELECT child.relname, a.attname, parent.relname FROM pg_constraint k "
        "JOIN pg_class child ON child.oid = k.conrelid JOIN pg_class parent ON parent.oid = k.confrelid "
        "JOIN pg_namespace n ON n.oid = child.relnamespace JOIN pg_namespace pn ON pn.oid = parent.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1] "
        "JOIN pg_attribute pa ON pa.attrelid = k.confrelid AND pa.attnum = k.confkey[1] "
        f"WHERE k.contype = 'f' AND n.nspname = {namespace} AND pn.nspname = {namespace} "
        "AND cardinality(k.conkey) = 1 AND pa.attname = 'id'",
        row_cap=10_000,
    ).rows
    index_rows = database.read(
        "SELECT DISTINCT c.relname, a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0] "
        f"WHERE n.nspname = {namespace}",
        row_cap=10_000,
    ).rows

    estimated = {name: int(rows or 0) for name, rows in table_rows}
    tables: dict[str, Table] = {}
    for table_name, column_name, column_type, in_primary_key in column_rows:
        if is_denied_table(table_name) or table_name not in estimated:
            continue
        table = tables.setdefault(
            table_name,
            Table(table_name, [], {}, [], estimated[table_name], []),
        )
        table.columns.append(column_name)
        table.column_types[column_name] = column_type
        if in_primary_key:
            table.primary_key.append(column_name)
    for table_name, column_name in index_rows:
        if table_name in tables:
            tables[table_name].indexed_leading_columns.append(column_name)
    configured = settings()
    scope_column = configured.scope.column if configured.scope else None
    for table in tables.values():
        table.label_column = _choose_label(table.columns)
        table.has_scope = scope_column is not None and scope_column in table.columns
        table.soft_delete_column = next((column for column in SOFT_DELETE_COLUMNS if column in table.columns), None)

    edges = [
        Edge(child, column, parent, "fk")
        for child, column, parent in foreign_key_rows
        if child in tables and parent in tables
    ]
    declared = {(edge.child, edge.column) for edge in edges}
    polymorphic_columns = {(link.table, link.id_column) for link in configured.polymorphic_links}
    for (child, column), parent in configured.implicit_references.items():
        if child in tables and parent in tables and column in tables[child].columns and (child, column) not in declared:
            edges.append(Edge(child, column, parent, "implicit"))
            declared.add((child, column))
    if configured.infer_references:
        for child, table in tables.items():
            for column in table.columns:
                if (child, column) in declared or (child, column) in polymorphic_columns or not column.endswith("_id"):
                    continue
                parent = _inferred_parent(column[:-3], tables)
                if parent:
                    edges.append(Edge(child, column, parent, "implicit"))
    for link in configured.polymorphic_links:
        if link.table not in tables:
            continue
        for type_value, parent in link.targets.items():
            if parent in tables:
                edges.append(Edge(link.table, link.id_column, parent, "polymorphic", link.type_column, type_value))

    return Schema(tables, edges, _detect_join_tables(tables, edges), built_at=time.time())


def _inferred_parent(stem: str, tables: dict[str, Table]) -> str | None:
    """customer -> customers, address -> addresses, category -> categories,
    or the bare stem; only tables keyed by a single id qualify."""
    candidates = (stem + "s", stem + "es", stem[:-1] + "ies" if stem.endswith("y") else None, stem)
    for candidate in candidates:
        if candidate and candidate in tables and tables[candidate].primary_key == ["id"]:
            return candidate
    return None


def load(database: Database, refresh: bool = False) -> Schema:
    cache_path = STATE_DIR / f"schema-{database.environment}.json"
    if not refresh and cache_path.exists():
        payload = json.loads(cache_path.read_text())
        fresh = time.time() - payload.get("built_at", 0) < SCHEMA_CACHE_MAX_AGE_SECONDS
        if payload.get("version") == SCHEMA_CACHE_VERSION and fresh:
            return Schema.from_json(payload)
    schema = introspect(database)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_path.write_text(json.dumps(schema.to_json()))
    return schema


def prompt_schema(schema: Schema, tables: list[str] | None = None) -> str:
    """Compact schema text for an LLM: one line per table, FK targets inline."""
    references = {(edge.child, edge.column): edge for edge in schema.edges if edge.kind != "polymorphic"}
    lines = []
    for name in sorted(tables or schema.tables):
        table = schema.tables[name]
        parts = []
        for column in table.columns:
            if is_secret_column(column):
                continue
            column_type = table.column_types[column].replace("timestamp with time zone", "timestamptz").replace("character varying", "varchar")
            text = f"{column} {column_type}"
            edge = references.get((name, column))
            if edge:
                text += f"→{edge.parent}"
            parts.append(text)
        lines.append(f"{name} (~{table.estimated_rows} rows): " + ", ".join(parts))
    polymorphic = [
        f"{link.table}.{link.id_column} where {link.type_column}=" + ", ".join(f"'{value}'→{target}" for value, target in link.targets.items())
        for link in settings().polymorphic_links
        if tables is None or link.table in tables
    ]
    if polymorphic:
        lines.append("Polymorphic references (no FK): " + "; ".join(polymorphic))
    return "\n".join(lines)


# ── value domains ──────────────────────────────────────────────────────────
# Enum-like text columns whose exact values the SQL model must see, or it
# invents plausible ones ('Pending' for 'pending'). A column qualifies by
# name; it is kept only if it really is small (DOMAIN_MAX_VALUES).
DOMAIN_COLUMN_PATTERN = re.compile(
    r"(^|_)(type|status|platform|source|visibility|scope|kind|state|role)$|^(entity_type|media_type|account_type)$"
)
DOMAIN_MAX_VALUES = 40
DOMAIN_CACHE_MAX_AGE_SECONDS = 24 * 3600


def _domain_columns(schema: Schema) -> list[tuple[str, str]]:
    columns = []
    for name, table in schema.tables.items():
        for column in table.columns:
            column_type = table.column_types[column]
            textual = column_type == "text" or column_type.startswith(("character varying", "character("))
            if not textual or is_secret_column(column):
                continue
            if DOMAIN_COLUMN_PATTERN.search(column) or (name in settings().domain_tables and column in ("name", "key")):
                columns.append((name, column))
    return columns


def value_domains(database: Database, schema: Schema, refresh: bool = False) -> dict[str, list[str]]:
    """'table.column' -> its values, most frequent first (cached daily)."""
    cache_path = STATE_DIR / f"domains-{database.environment}.json"
    if not refresh and cache_path.exists() and time.time() - cache_path.stat().st_mtime < DOMAIN_CACHE_MAX_AGE_SECONDS:
        return json.loads(cache_path.read_text())

    def read_domain(table: str, column: str) -> tuple[str, list[str]] | None:
        quoted = quote_identifier(column)
        rows = database.read(
            f"SELECT {quoted}, COUNT(*) AS n FROM {schema.tables[table].reference} WHERE {quoted} IS NOT NULL AND {quoted} <> '' "
            f"GROUP BY {quoted} ORDER BY n DESC LIMIT {DOMAIN_MAX_VALUES + 1}",
            row_cap=DOMAIN_MAX_VALUES + 1,
        ).rows
        if not rows or len(rows) > DOMAIN_MAX_VALUES:
            return None
        return f"{table}.{column}", [str(value) for value, _ in rows]

    with ThreadPoolExecutor(max_workers=6) as pool:
        found = [result for result in pool.map(lambda pair: read_domain(*pair), _domain_columns(schema)) if result]
    domains = dict(sorted(found))
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_path.write_text(json.dumps(domains))
    return domains
