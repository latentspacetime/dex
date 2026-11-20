import pytest

from dex import links
from dex.router import local_route


@pytest.mark.parametrize(
    "text, expected",
    [
        ("orders/981?product=7", [("orders", "981"), ("products", "7")]),
        ("https://shop.example.com/customers/42/orders/981?tab=items", [("customers", "42"), ("orders", "981")]),
        ("/customers/12/edit?returnTo=x", [("customers", "12")]),
        ("shop.example.com/catalog?product=255914", [("products", "255914")]),
        # UUID and text ids pass through; the explorer checks them against the id type.
        ("app.example.com/orders/3f1c2a7e-8b9d-4c5e-a1f2-0123456789ab", [("orders", "3f1c2a7e-8b9d-4c5e-a1f2-0123456789ab")]),
    ],
)
def test_app_urls_name_their_rows(text, expected):
    assert [(reference.table, reference.id) for reference in links.parse(text)] == expected


@pytest.mark.parametrize(
    "text",
    ["22948", "blue scarf", "s3://bucket/uploads/orders/300/receipt.png", "suborders/5", "orders/" + "9" * 121],
)
def test_non_links_name_nothing(text):
    assert links.parse(text) == []


@pytest.mark.parametrize(
    "text, mode",
    [
        ("SELECT id FROM accounts", "sql"),
        ("select 1", "sql"),
        ("UPDATE orders SET status = 'shipped' WHERE id = 1", "sql"),
        ("delete from tags where id = 3", "sql"),
        ("explain select * from patients", "sql"),
        ('update "order" set x = 1', "sql"),
        ("orders/981?product=7", "search"),
        ("22948", "search"),
    ],
)
def test_clear_inputs_are_routed_without_a_model(text, mode):
    decided = local_route(text)
    assert decided is not None and decided.mode == mode and decided.decided_by == "rule"


@pytest.mark.parametrize(
    "text",
    ["update customer 46 email to 'a@b.co'", "delete order 285228",
     "select the best selling products", "how many orders does acme have", "describe products"],
)
def test_english_that_starts_like_sql_goes_to_the_model(text):
    assert local_route(text) is None
