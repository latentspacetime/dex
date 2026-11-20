"""Explain an empty result: did the query filter on values that do not exist?

A model that guesses a value ('auto_refine', 'Customer') gets 0 rows and a
plausible-looking empty table. For every `column = 'text'`, `LIKE` and `IN`
filter, this checks the data: the column's known values, a case-insensitive
match, and near matches. The lines go back to the model for one retry and
are shown beside the result.
"""

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from dex.db import Database, QueryError, quote_identifier
from dex.guard import DIALECT

MAX_FILTERS_CHECKED = 6
NEAR_MATCHES_SHOWN = 8
KNOWN_VALUES_SHOWN = 25


@dataclass
class FilterCheck:
    lines: list[str] = field(default_factory=list)
    found_problem: bool = False


def _string_filters(tree: exp.Expression) -> list[tuple[exp.Column, str, str]]:
    """(column, operator, literal) for text comparisons in the query."""
    filters = []
    for node in tree.find_all(exp.EQ, exp.Like, exp.ILike, exp.In):
        if isinstance(node, exp.In):
            if isinstance(node.this, exp.Column):
                for value in node.expressions:
                    if isinstance(value, exp.Literal) and value.is_string:
                        filters.append((node.this, "IN", value.this))
            continue
        left, right = node.this, node.expression
        if isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            left, right = right, left
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal) and right.is_string:
            operator = "LIKE" if isinstance(node, (exp.Like, exp.ILike)) else "="
            filters.append((left, operator, right.this))
    return filters[:MAX_FILTERS_CHECKED]


def _resolve_table(column: exp.Column, aliases: dict[str, str], tables: set[str]) -> str | None:
    if column.table:
        return aliases.get(column.table.lower())
    return next(iter(tables)) if len(tables) == 1 else None


def _near_term(value: str) -> str:
    words = [word for word in value.replace("%", " ").replace("_", " ").split() if len(word) >= 3]
    return max(words, key=len) if words else value.strip("%")


def check_filters(sql: str, database: Database, domains: dict[str, list[str]]) -> FilterCheck:
    check = FilterCheck()
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except sqlglot.errors.SqlglotError:
        return check
    aliases = {table.alias_or_name.lower(): table.name.lower() for table in tree.find_all(exp.Table)}
    tables = {table.name.lower() for table in tree.find_all(exp.Table)}
    for column, operator, value in _string_filters(tree):
        table = _resolve_table(column, aliases, tables)
        if table is None:
            continue
        name = f"{table}.{column.name}"
        shown = f"{name} {operator} '{value}'"
        known = domains.get(name)
        if known is not None and operator in ("=", "IN"):
            if value in known:
                continue
            check.found_problem = True
            case_match = [candidate for candidate in known if candidate.lower() == value.lower()]
            if case_match:
                check.lines.append(f"{shown}: no such value; the stored value is '{case_match[0]}' (case differs).")
            else:
                check.lines.append(f"{shown}: no such value. All values: {', '.join(known[:KNOWN_VALUES_SHOWN])}.")
            continue
        # The model's own SQL named these; only dex's quoting reaches the server.
        reference = quote_identifier(table)
        text_column = f"{quote_identifier(column.name)}::text"
        try:
            if operator == "LIKE":
                matches = database.read(f"SELECT COUNT(*) FROM {reference} WHERE {text_column} LIKE %s", (value,)).rows[0][0]
            else:
                matches = database.read(f"SELECT COUNT(*) FROM {reference} WHERE {text_column} = %s", (value,)).rows[0][0]
            if matches:
                continue
            check.found_problem = True
            near = database.read(
                f"SELECT DISTINCT {text_column} FROM {reference} WHERE {text_column} ILIKE %s LIMIT {NEAR_MATCHES_SHOWN}",
                ("%" + _near_term(value) + "%",),
            ).rows
            if near:
                check.lines.append(f"{shown}: matches nothing. Values containing '{_near_term(value)}': "
                                   + ", ".join(f"'{row[0]}'" for row in near) + ".")
            else:
                check.lines.append(f"{shown}: matches nothing, and no value contains '{_near_term(value)}'.")
        except QueryError:  # an unknown column or type: the check simply has nothing to say
            continue
    return check
