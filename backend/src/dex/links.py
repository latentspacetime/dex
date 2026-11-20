"""Turn an app URL (or a pasted fragment of one) into the rows it names.

Patterns come from the config, so dex knows your app's routes:

  [[links.paths]]
  pattern = "orders/([\\w-]+)"  # one capture group: the id (digits, a UUID, a slug)
  table = "orders"

  [links.query]
  customer = "customers"        # ?customer=42 -> customers #42

With that, `https://shop.example.com/orders/981?customer=42` names
orders #981 and customers #42. Nothing is parsed when no patterns are set.
"""

import re
from dataclasses import dataclass
from functools import cache
from urllib.parse import parse_qs, urlsplit

from dex.config import settings


@dataclass(frozen=True)
class LinkReference:
    table: str
    id: str
    source: str  # the URL part that named it, shown beside the hit


MAX_ID_LENGTH = 120


@cache
def _path_patterns() -> tuple[tuple[re.Pattern, str], ...]:
    # Anchored at a path segment so "orders/1" never matches "suborders/1".
    return tuple((re.compile(r"(?:^|/)" + pattern.lstrip("/") + r"(?=/|$|\?|#)"), table)
                 for pattern, table in settings().link_paths)


def looks_like_link(text: str) -> bool:
    # Object-store paths (s3://, gs://) are text to search, not app routes.
    stripped = text.strip()
    return " " not in stripped and ("/" in stripped or "?" in stripped) and not stripped.startswith(("s3://", "gs://"))


def parse(text: str) -> list[LinkReference]:
    """References in the order the URL names them; empty when nothing matches."""
    stripped = text.strip()
    if not looks_like_link(stripped) or not (settings().link_paths or settings().link_query):
        return []
    split = urlsplit(stripped if "://" in stripped else "//placeholder/" + stripped.lstrip("/"))
    references: list[LinkReference] = []
    seen: set[tuple[str, str]] = set()

    def add(table: str, raw_id: str, source: str) -> None:
        # Whether the id fits the table's id type is the explorer's call.
        if not raw_id or len(raw_id) > MAX_ID_LENGTH:
            return
        key = (table, raw_id)
        if key not in seen:
            seen.add(key)
            references.append(LinkReference(table, raw_id, source))

    matches = sorted(((match.start(), table, match) for pattern, table in _path_patterns()
                      for match in pattern.finditer(split.path)), key=lambda found: found[0])
    for _, table, match in matches:
        add(table, match.group(1), match.group(0).strip("/"))
    for name, values in parse_qs(split.query).items():
        table = settings().link_query.get(name)
        if table:
            for value in values:
                add(table, value, f"{name}={value}")
    return references
