from dex.index import deep_sources
from dex.schema import Schema, Table, _inferred_parent


def _table(name, columns, primary_key=("id",)):
    return Table(name, list(columns), dict(columns), list(primary_key), 100, [])


SCHEMA = Schema(
    tables={
        "customers": _table("customers", {"id": "bigint", "name": "character varying(100)", "notes": "text"}),
        "addresses": _table("addresses", {"id": "bigint", "customer_id": "bigint", "line1": "character varying(100)"}),
        "categories": _table("categories", {"id": "bigint", "description": "character varying(500)"}),
        "orders": _table("orders", {"id": "uuid", "customer_id": "bigint", "status": "character varying(20)",
                                    "comment": "text", "api_key": "text", "payload": "jsonb"}),
        "order_items": _table("order_items", {"order_id": "uuid", "sku": "character varying(40)"}, ("order_id", "sku")),
    },
    edges=[],
)


def test_id_columns_resolve_to_plural_tables():
    assert _inferred_parent("customer", SCHEMA.tables) == "customers"
    assert _inferred_parent("address", SCHEMA.tables) == "addresses"
    assert _inferred_parent("category", SCHEMA.tables) == "categories"
    assert _inferred_parent("warehouse", SCHEMA.tables) is None
    # A composite-key table is never a reference target.
    assert _inferred_parent("order_item", SCHEMA.tables) is None


def test_deep_search_picks_written_text_and_skips_secrets():
    sources = [(table, column) for table, column, _ in deep_sources(SCHEMA)]
    assert ("orders", "comment") in sources
    assert ("customers", "notes") in sources
    assert ("categories", "description") in sources
    assert ("orders", "api_key") not in sources
    assert ("orders", "status") not in sources
    # A short varchar is a code, not written text.
    assert ("addresses", "line1") not in sources
    # Comments rank above descriptions.
    assert sources.index(("orders", "comment")) < sources.index(("categories", "description"))


def test_tokens_are_matched_only_against_ids_that_can_hold_them():
    orders, customers = SCHEMA.tables["orders"], SCHEMA.tables["customers"]
    assert customers.accepts("id", "42") and not customers.accepts("id", "abc")
    assert not customers.accepts("id", "9" * 20)  # past bigint
    assert orders.accepts("id", "3f1c2a7e-8b9d-4c5e-a1f2-0123456789ab") and not orders.accepts("id", "42")
    assert customers.accepts("notes", "anything")
    assert not orders.accepts("payload", "x")  # jsonb is never an identifier
