"""Decide which mode an input belongs to: search, deep, ask, change, or sql.

Clear cases are decided locally and free (SQL keywords, app URLs, single
tokens). Everything else goes to the liquid/d1-ultra decision model, unless
[ask].router is false. The UI
uses the verdict to light up a tab and to follow it on Enter; Ask uses it to
short-circuit lookups.
"""

import re
from dataclasses import dataclass

from dex import links, llm
from dex.config import settings

# SQL by shape, not by first word: "update customer 46 email …" and
# "delete order 285228" are English and belong to the model.
SQL_SHAPE = re.compile(
    r"^\s*(select\s.+\sfrom\s|select\s+[\d'\"(]|with\s+\w+\s+as\s*\(|explain\s.*select\s"
    r"|update\s+\"?\w+\"?\s+set\s|delete\s+from\s|insert\s+into\s)",
    re.IGNORECASE | re.DOTALL,
)
# The UI follows a d1-ultra verdict only at or above this confidence; below
# it the input stays in the tab it was typed in.
FOLLOW_CONFIDENCE = 0.8

CRITERIA = {
    "search": "The name, id, code, email, path or short keyword of a specific record to find "
              "(a customer, product, order, account, user, tag).",
    "deep": "A verbatim fragment of text someone wrote or generated (a comment, a note, a description, "
            "a message, a log line) to find where that text appears.",
    "ask": "A question or instruction that needs a computed answer: counts, totals, lists matching "
           "conditions, filters, time ranges, comparisons, rankings.",
    "change": "A request to modify data: set, update, change, rename, delete, restore, mark or fix a record.",
}


@dataclass
class Route:
    mode: str           # search | deep | ask | change | sql
    confidence: float
    decided_by: str     # "rule" | "d1-ultra"
    elapsed_ms: float = 0.0
    reason: str = ""


def local_route(text: str) -> Route | None:
    stripped = text.strip()
    if not stripped:
        return Route("search", 1.0, "rule", reason="empty")
    if SQL_SHAPE.match(stripped):
        return Route("sql", 1.0, "rule", reason="SQL statement")
    if links.parse(stripped):
        return Route("search", 1.0, "rule", reason="app URL")
    if " " not in stripped:
        return Route("search", 1.0, "rule", reason="single token")
    return None


def route(text: str, llm_allowed: bool) -> Route:
    """llm_allowed: False for an environment whose typed input must not leave
    the machine; only the local rules decide."""
    decided = local_route(text)
    if decided:
        return decided
    if not (llm_allowed and settings().router_enabled):
        return Route("search", 0.0, "rule", reason="router off")
    decision = llm.decide(
        state=f"Text typed into a database explorer's search box: {text.strip()}",
        instructions="What does the person want done with this input?",
        criteria=CRITERIA,
    )
    return Route(decision.choice, decision.confidence, "d1-ultra", decision.elapsed_ms)
