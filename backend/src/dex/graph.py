"""Universal id lookup and the entity card: a row plus everything around it."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from dex.config import settings
from dex.db import Database, quote_identifier, quote_literal
from dex.index import Hit
from dex.schema import Edge, Schema

# Columns holding identifiers minted outside this database (UUIDs, SKUs,
# tracking or payment ids). A typed token is also matched against these and
# against [search].external_id_columns.
DEFAULT_EXTERNAL_ID_COLUMNS = ("uuid", "external_id", "external_ref", "reference", "tracking_id", "sku", "code")
# Unindexed external-id columns are scanned only on tables at most this big;
# a scan of 50k rows takes a few milliseconds.
UNINDEXED_SCAN_ROW_LIMIT = 60_000

CHILD_SAMPLE_SIZE = 6
# Child groups beyond this many (by size) show only their count; a hub row with
# dozens of child tables would otherwise double the card's query time.
CHILD_GROUPS_SAMPLED = 12
LINKED_SAMPLE_SIZE = 12
PARALLEL_QUERIES = 8


@dataclass
class Node:
    table: str
    id: Any
    label: str
    via: str = ""
    scope: str = ""


@dataclass
class ChildGroup:
    table: str
    via: str
    count: int
    items: list[Node] = field(default_factory=list)


@dataclass
class Card:
    table: str
    id: Any
    label: str
    scope: str
    columns: list[str]
    values: list[Any]
    parents: list[Node]
    lineage: list[Node]
    linked: list[ChildGroup]
    children: list[ChildGroup]
    elapsed_ms: float

    def to_json(self) -> dict:
        return asdict(self)


def label_sql(schema: Schema, table: str, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    overrides = settings().label_overrides
    if table in overrides and not alias:
        return overrides[table]
    label_column = schema.tables[table].label_column
    if label_column:
        # ::text: a UNION of labels from many tables needs one column type.
        return f"({prefix}{quote_identifier(label_column)})::text"
    return "NULL::text"


def _fallback_label(table: str, row_id: Any, label: Any) -> str:
    if label not in (None, ""):
        return str(label)
    return f"{table} #{row_id}"


class Explorer:
    def __init__(self, database: Database, schema: Schema, scopes: dict[str, str]):
        self.database = database
        self.schema = schema
        self.scopes = scopes
        scope = settings().scope
        self.scope_column = scope.column if scope else None
        self.pool = ThreadPoolExecutor(max_workers=PARALLEL_QUERIES, thread_name_prefix="dex-graph")

    def _scope(self, scope_id: Any) -> str:
        return self.scopes.get("" if scope_id is None else str(scope_id), "")

    def _scope_sql(self, table: str) -> str:
        return f"{quote_identifier(self.scope_column)}::text" if self.schema.tables[table].has_scope else "NULL::text"

    # ── id lookup ────────────────────────────────────────────────────────
    def _external_id_columns(self) -> list[tuple[str, str]]:
        columns = []
        for name, table in self.schema.tables.items():
            for column in dict.fromkeys(settings().external_id_columns + DEFAULT_EXTERNAL_ID_COLUMNS):
                if column not in table.columns:
                    continue
                if column in table.indexed_leading_columns or table.estimated_rows <= UNINDEXED_SCAN_ROW_LIMIT:
                    columns.append((name, column))
        return columns

    def lookup_identifier(self, token: str) -> list[Hit]:
        """Find every row whose primary key or external identifier equals token.

        Each comparison casts the token to the column's own type (keeping
        the index usable); a table whose id cannot hold the token at all (a
        word against an integer id, a non-UUID against a uuid) is skipped."""
        token = token.strip()
        if not token or len(token) > 120:
            return []
        selects: list[str] = []
        parameters: list[Any] = []
        for name, table in self.schema.tables.items():
            if not self.schema.has_id_key(name) or not table.accepts("id", token):
                continue
            selects.append(
                f"SELECT {quote_literal(name)}::text, id::text, {self._scope_sql(name)}, {label_sql(self.schema, name)}, 'id'::text "
                f"FROM {table.reference} WHERE id = {table.parameter('id')}"
            )
            parameters.append(token)
        primary_key_count = len(selects)
        for name, column in self._external_id_columns():
            table = self.schema.tables[name]
            if not self.schema.has_id_key(name) or not table.accepts(column, token):
                continue
            selects.append(
                f"(SELECT {quote_literal(name)}::text, id::text, {self._scope_sql(name)}, {label_sql(self.schema, name)}, "
                f"{quote_literal(column)}::text FROM {table.reference} WHERE {quote_identifier(column)} = {table.parameter(column)} LIMIT 5)"
            )
            parameters.append(token)
        if not selects:
            return []
        # Primary keys answer in one index probe each; external-id columns
        # may scan small tables. Two parallel round trips keep the fast half
        # from waiting on the slow one's server time.
        split = primary_key_count
        halves = [(selects[:split], parameters[:split]), (selects[split:], parameters[split:])]
        futures = [self.pool.submit(self.database.read, " UNION ALL ".join(part), tuple(values), 200)
                   for part, values in halves if part]
        rows = [row for future in futures for row in future.result().rows]
        # Configured tables first, then the largest: a bigger table is more
        # often the one a person is holding an id for.
        priority = {name: rank for rank, name in enumerate(settings().priority_tables)}
        hits = [
            Hit(table, str(row_id), "" if scope_id is None else str(scope_id), self._scope(scope_id),
                _fallback_label(table, row_id, label), "id", column="" if matched == "id" else matched)
            for table, row_id, scope_id, label, matched in rows
        ]
        hits.sort(key=lambda hit: (hit.column != "", priority.get(hit.table, len(priority)),
                                   -self.schema.tables[hit.table].estimated_rows))
        return hits

    def resolve_references(self, references) -> list[Hit]:
        """Hits for rows a URL names (links.parse), in URL order; a
        reference to a missing row is dropped."""
        known = [reference for reference in references if reference.table in self.schema.tables]
        nodes = self._labels_for([(reference.table, reference.id, reference.source) for reference in known])
        found = {(node.table, str(node.id)): node for node in nodes}
        hits = []
        for reference in known:
            node = found.get((reference.table, str(reference.id)))
            if node:
                hits.append(Hit(node.table, str(node.id), "", node.scope, node.label, "link", column=f"from URL {node.via}"))
        return hits

    # ── entity card ──────────────────────────────────────────────────────
    def _labels_for(self, references: list[tuple[str, Any, str]]) -> list[Node]:
        """One round trip for many (table, id, via) parent labels."""
        selects, parameters = [], []
        for table, row_id, via in references:
            parent = self.schema.tables[table]
            if not parent.accepts("id", str(row_id)):
                continue  # a value that cannot be this table's id names no row
            selects.append(f"SELECT {quote_literal(table)}::text, id::text, {self._scope_sql(table)}, {label_sql(self.schema, table)}, "
                           f"%s::text FROM {parent.reference} WHERE id = {parent.parameter('id')}")
            parameters.extend([via, str(row_id)])
        if not selects:
            return []
        rows = self.database.read(" UNION ALL ".join(selects), tuple(parameters), row_cap=500).rows
        return [Node(table, row_id, _fallback_label(table, row_id, label), via, self._scope(scope_id))
                for table, row_id, scope_id, label, via in rows]

    def _child_condition(self, edge: Edge, parent_id: Any, scope_id: Any) -> tuple[str, list[Any]]:
        child = self.schema.tables[edge.child]
        conditions = [f"{quote_identifier(edge.column)} = {child.parameter(edge.column)}"]
        parameters: list[Any] = [str(parent_id)]
        if edge.kind == "polymorphic":
            conditions.append(f"{quote_identifier(edge.type_column)} = {child.parameter(edge.type_column)}")
            parameters.append(edge.type_value)
            # Polymorphic tables are often indexed by the scope column rather
            # than (type, id); scoping turns a table scan into an index range.
            if scope_id is not None and child.has_scope:
                conditions.append(f"{quote_identifier(self.scope_column)} = {child.parameter(self.scope_column)}")
                parameters.append(str(scope_id))
        return " AND ".join(conditions), parameters

    def _child_groups(self, edges: list[Edge], parent_id: Any, scope_id: Any) -> list[ChildGroup]:
        """Two round trips for any number of child edges: all counts, then
        samples for the largest CHILD_GROUPS_SAMPLED groups."""
        if not edges:
            return []
        conditions = [self._child_condition(edge, parent_id, scope_id) for edge in edges]
        count_selects, count_parameters = [], []
        for position, (edge, (where, parameters)) in enumerate(zip(edges, conditions)):
            count_selects.append(f"SELECT {position}, COUNT(*) FROM {self.schema.tables[edge.child].reference} WHERE {where}")
            count_parameters.extend(parameters)
        counts = {int(position): int(count) for position, count in
                  self.database.read(" UNION ALL ".join(count_selects), tuple(count_parameters), row_cap=1000).rows}
        groups: dict[int, ChildGroup] = {}
        for position, edge in enumerate(edges):
            if counts.get(position):
                via = edge.column if edge.kind != "polymorphic" else f"{edge.type_column}={edge.type_value}"
                groups[position] = ChildGroup(edge.child, via, counts[position])

        sampled = sorted(groups, key=lambda position: -groups[position].count)[:CHILD_GROUPS_SAMPLED]
        sample_selects, sample_parameters = [], []
        for position in sampled:
            edge = edges[position]
            if not self.schema.has_id_key(edge.child):
                continue
            where, parameters = conditions[position]
            sample_selects.append(
                f"(SELECT {position}, id::text, {label_sql(self.schema, edge.child)} FROM {self.schema.tables[edge.child].reference} "
                f"WHERE {where} ORDER BY id DESC LIMIT {CHILD_SAMPLE_SIZE})"
            )
            sample_parameters.extend(parameters)
        if sample_selects:
            for position, row_id, label in self.database.read(
                " UNION ALL ".join(sample_selects), tuple(sample_parameters), row_cap=1000
            ).rows:
                group = groups[int(position)]
                group.items.append(Node(group.table, row_id, _fallback_label(group.table, row_id, label)))
        return list(groups.values())

    def _linked_group(self, join_table: str, near: Edge, far: Edge, row_id: Any) -> ChildGroup | None:
        far_table = far.parent
        join = self.schema.tables[join_table]
        near_condition = f"{quote_identifier(near.column)} = {join.parameter(near.column)}"
        rows = self.database.read(
            f"SELECT far.id::text, {label_sql(self.schema, far_table, 'far')} FROM {join.reference} j "
            f"JOIN {self.schema.tables[far_table].reference} far ON far.id = j.{quote_identifier(far.column)} WHERE j.{near_condition} "
            f"ORDER BY far.id DESC LIMIT {LINKED_SAMPLE_SIZE + 1}",
            (str(row_id),),
        ).rows
        if not rows:
            return None
        items = [Node(far_table, far_id, _fallback_label(far_table, far_id, label), via=join_table)
                 for far_id, label in rows[:LINKED_SAMPLE_SIZE]]
        count = len(rows)
        if count > LINKED_SAMPLE_SIZE:
            count = self.database.read(
                f"SELECT COUNT(*) FROM {join.reference} WHERE {near_condition}", (str(row_id),)
            ).rows[0][0]
        return ChildGroup(far_table, join_table, int(count), items)

    def _resolve_lineage(self, table: str, row: dict[str, Any]) -> list[Node]:
        """Follow the configured [schema.lineage] paths for this table.

        Each path is one joined query (one round trip) rather than a query per
        hop; the first path that reaches its end wins."""
        for path in settings().lineage.get(table, ()):
            first_column = path[0][0]
            if row.get(first_column) is None:
                continue
            selects, joins = [], []
            for step, (column, parent_table) in enumerate(path):
                alias = f"s{step}"
                selects.append(f"{alias}.id::text, {label_sql(self.schema, parent_table, alias)}")
                reference = self.schema.tables[parent_table].reference
                if step == 0:
                    joins.append(f"{reference} {alias}")
                else:
                    joins.append(f"JOIN {reference} {alias} ON {alias}.id = s{step - 1}.{quote_identifier(column)}")
            first = self.schema.tables[path[0][1]]
            sql = f"SELECT {', '.join(selects)} FROM {' '.join(joins)} WHERE s0.id = {first.parameter('id')}"
            rows = self.database.read(sql, (str(row[first_column]),)).rows
            if not rows:
                continue
            values = rows[0]
            return [
                Node(parent_table, values[2 * step], _fallback_label(parent_table, values[2 * step], values[2 * step + 1]), via=column)
                for step, (column, parent_table) in enumerate(path)
            ]
        return []

    def card(self, table: str, row_id: Any) -> Card:
        if table not in self.schema.tables or not self.schema.has_id_key(table):
            raise KeyError(f"{table} is not a table with an id key")
        row_id = str(row_id)
        info = self.schema.tables[table]
        if not info.accepts("id", row_id):
            raise LookupError(f"{row_id!r} cannot be an id of {table} ({info.column_types['id']})")
        started = time.perf_counter()
        result = self.database.read(f"SELECT * FROM {info.reference} WHERE id = {info.parameter('id')}", (row_id,))
        if not result.rows:
            raise LookupError(f"{table} #{row_id} not found")
        row = dict(zip(result.columns, result.rows[0]))
        scope_id = row.get(self.scope_column) if self.schema.tables[table].has_scope else None
        label_column = self.schema.tables[table].label_column
        label = _fallback_label(table, row_id, row.get(label_column) if label_column else None)

        parent_refs = []
        for edge in self.schema.outgoing(table):
            value = row.get(edge.column)
            if value is None:
                continue
            if edge.kind == "polymorphic" and row.get(edge.type_column) != edge.type_value:
                continue
            parent_refs.append((edge.parent, value, edge.column))

        child_edges = [edge for edge in self.schema.incoming(table) if edge.child not in self.schema.join_tables]
        join_sides = [(name, near, far) for name, (left, right) in self.schema.join_tables.items()
                      for near, far in ((left, right), (right, left)) if near.parent == table]

        parents_future = self.pool.submit(self._labels_for, parent_refs)
        lineage_future = self.pool.submit(self._resolve_lineage, table, row)
        children_future = self.pool.submit(self._child_groups, child_edges, row_id, scope_id)
        linked_futures = [self.pool.submit(self._linked_group, name, near, far, row_id) for name, near, far in join_sides]

        lineage = lineage_future.result()
        children = children_future.result()
        children.sort(key=lambda group: (-min(group.count, 10_000), group.table))
        linked = [group for future in linked_futures if (group := future.result())]
        return Card(
            table=table, id=row_id, label=label, scope=self._scope(scope_id),
            columns=result.columns, values=result.rows[0],
            parents=parents_future.result(), lineage=lineage, linked=linked, children=children,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
