"""The home screen: what is happening in the database right now.

Activity tables are [home].tables from the config, or else the largest
tables whose creation timestamp is cheap to count (indexed, or the table is
small enough to scan).
"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from dex.config import settings
from dex.db import Database, quote_identifier, quote_literal
from dex.graph import label_sql
from dex.schema import Schema

CACHE_SECONDS = 60
SPARKLINE_DAYS = 14
TOP_SCOPES = 8
RECENT_ROWS = 6
ACTIVITY_TABLES = 6
RECENT_TABLES = 2
TIMESTAMP_COLUMNS = ("created_at", "created", "created_on", "inserted_at", "date_created", "creation_time", "createdAt")
# An unindexed timestamp is scanned only on tables at most this big.
UNINDEXED_SCAN_ROW_LIMIT = 300_000


@dataclass
class Overview:
    activity: list[dict[str, Any]]       # {label, day, week}
    per_day: list[dict[str, Any]]        # {day, n}, oldest first, for activity[0]
    per_day_label: str
    top_scopes: list[dict[str, Any]]     # {id, name, n} by activity[0] rows this week
    scope_table: str
    recent: list[dict[str, Any]]         # {table, rows: [{id, label, scope, at}]}
    largest_tables: list[dict[str, Any]]  # {table, rows}
    elapsed_ms: float = 0.0
    generated_at: float = field(default_factory=time.time)


class OverviewBuilder:
    def __init__(self, database: Database, schema: Schema, pool: ThreadPoolExecutor, scopes: dict[str, str]):
        self.database = database
        self.schema = schema
        self.pool = pool
        self.scopes = scopes
        self._cached: Overview | None = None
        self.activity_tables = self._activity_tables()

    def _timestamp_column(self, name: str) -> str | None:
        """The creation column, if it is a real timestamp: some schemas keep
        dates as text, which would compare as strings against now()."""
        table = self.schema.tables[name]
        for column in TIMESTAMP_COLUMNS:
            if column in table.columns and table.column_types[column].startswith(("timestamp", "date")):
                return column
        return None

    def _activity_tables(self) -> list[tuple[str, str]]:
        """(table, timestamp column), busiest first."""
        configured = [name for name in settings().home_tables if name in self.schema.tables]
        candidates = configured or sorted(self.schema.tables, key=lambda name: -self.schema.tables[name].estimated_rows)
        chosen = []
        for name in candidates:
            column = self._timestamp_column(name)
            if column is None or not self.schema.has_id_key(name):
                continue
            table = self.schema.tables[name]
            cheap = column in table.indexed_leading_columns or table.estimated_rows <= UNINDEXED_SCAN_ROW_LIMIT
            if configured or cheap:
                chosen.append((name, column))
            if len(chosen) == ACTIVITY_TABLES:
                break
        return chosen

    def _activity_sql(self) -> str:
        return "\nUNION ALL\n".join(
            f"SELECT {quote_literal(name)}::text, COUNT(*) FILTER (WHERE {quote_identifier(column)} >= now() - interval '1 day'), "
            f"COUNT(*) FROM {self.schema.tables[name].reference} WHERE {quote_identifier(column)} >= now() - interval '7 days'"
            for name, column in self.activity_tables
        )

    def _recent_sql(self, name: str, column: str) -> str:
        table = self.schema.tables[name]
        scope_sql = f"{quote_identifier(settings().scope.column)}::text" if table.has_scope else "NULL::text"
        # Newest by creation time: ids may be UUIDs or text, which sort by nothing meaningful.
        return (f"SELECT id::text, {label_sql(self.schema, name)}, {scope_sql}, {quote_identifier(column)} FROM {table.reference} "
                f"WHERE {table.live_condition()} AND {quote_identifier(column)} IS NOT NULL "
                f"ORDER BY {quote_identifier(column)} DESC LIMIT {RECENT_ROWS}")

    def build(self, refresh: bool = False) -> dict:
        if self._cached and not refresh and time.time() - self._cached.generated_at < CACHE_SECONDS:
            return asdict(self._cached)
        started = time.perf_counter()
        futures = {}
        if self.activity_tables:
            busiest, column = self.activity_tables[0]
            futures["activity"] = self.pool.submit(self.database.read, self._activity_sql())
            day_sql = f"{quote_identifier(column)}::date"  # UTC: every read sets the session time zone
            futures["per_day"] = self.pool.submit(
                self.database.read,
                f"SELECT {day_sql} AS day, COUNT(*) FROM {self.schema.tables[busiest].reference} "
                f"WHERE {quote_identifier(column)} >= current_date - {SPARKLINE_DAYS - 1} GROUP BY 1 ORDER BY 1",
            )
            scope = settings().scope
            if scope and self.schema.tables[busiest].has_scope and scope.table in self.schema.tables:
                futures["scopes"] = self.pool.submit(
                    self.database.read,
                    f"SELECT s.id::text, s.{quote_identifier(scope.label)}::text, COUNT(*) AS n FROM {self.schema.tables[busiest].reference} t "
                    f"JOIN {self.schema.tables[scope.table].reference} s ON s.id::text = t.{quote_identifier(scope.column)}::text "
                    f"WHERE t.{quote_identifier(column)} >= now() - interval '7 days' "
                    f"GROUP BY 1, 2 ORDER BY n DESC LIMIT {TOP_SCOPES}",
                )
            for name, column in self.activity_tables[:RECENT_TABLES]:
                futures[f"recent:{name}"] = self.pool.submit(self.database.read, self._recent_sql(name, column))
        rows = {name: future.result().rows for name, future in futures.items()}
        largest = sorted(self.schema.tables.values(), key=lambda table: -table.estimated_rows)[:10]
        busiest_name = self.activity_tables[0][0] if self.activity_tables else ""
        scope = settings().scope
        self._cached = Overview(
            activity=[{"label": label, "day": int(day or 0), "week": int(week or 0)} for label, day, week in rows.get("activity", [])],
            per_day=[{"day": str(day), "n": int(n)} for day, n in rows.get("per_day", [])],
            per_day_label=busiest_name,
            top_scopes=[{"id": scope_id, "name": "" if name is None else str(name), "n": int(n)}
                        for scope_id, name, n in rows.get("scopes", [])],
            scope_table=scope.table if scope else "",
            recent=[
                {"table": name, "rows": [{"id": row_id, "label": "" if label is None else str(label),
                                          "scope": self.scopes.get(scope_id or "", ""), "at": str(at)}
                                         for row_id, label, scope_id, at in rows[f"recent:{name}"]]}
                for name, _ in self.activity_tables[:RECENT_TABLES]
            ],
            largest_tables=[{"table": table.name, "rows": table.estimated_rows} for table in largest],
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return asdict(self._cached)
