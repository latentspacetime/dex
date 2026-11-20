import pytest

from dex.db import redact
from dex.guard import GuardRejection, StatementKind, guard_read, inspect_statement


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM accounts",
        "WITH recent AS (SELECT id FROM orders) SELECT * FROM recent",
        "SELECT 1 UNION SELECT 2",
        "EXPLAIN SELECT id FROM accounts",
        "EXPLAIN (ANALYZE, BUFFERS) SELECT id FROM accounts",
        'SELECT "order", "group" FROM counters WHERE "order" = \'first\'',
        "SELECT * FROM public.patients p JOIN information_schema.columns c ON true",
        "SELECT payload->>'op' FROM robot_commands WHERE created_at >= now() - interval '1 day'",
        "SELECT current_setting('transaction_read_only')",
    ],
)
def test_reads_are_allowed(sql):
    assert inspect_statement(sql).kind is StatementKind.READ


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("SELECT 1; DROP TABLE accounts", "exactly one statement"),
        ("DROP TABLE accounts", "DROP statements are not allowed"),
        ("TRUNCATE accounts", "not allowed"),
        ("SELECT * FROM api_tokens", "holds credentials"),
        ("SELECT a.user_id FROM api_tokens a", "holds credentials"),
        ("SELECT control_secret AS harmless FROM webhooks", "holds a secret"),
        ("SELECT facebook_oauth_token FROM integrations", "holds a secret"),
        ("SELECT * FROM accounts FOR UPDATE", "locking reads"),
        ("SELECT 'unterminated", "could not parse"),
        ("", "empty statement"),
        # Supabase and Neon keep credentials in their own schemas.
        ("SELECT email FROM auth.users", "schema auth"),
        ("SELECT * FROM neon_auth.session", "schema neon_auth"),
        ("SELECT * FROM public.x JOIN vault.decrypted_secrets s ON true", "schema vault"),
        # A read-only transaction still allows these; the guard does not.
        ("SELECT set_config('default_transaction_read_only', 'off', false)", "set_config"),
        ("SELECT query_to_xml('select * from auth.users', true, true, '')", "query_to_xml"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT pg_terminate_backend(1)", "pg_terminate_backend"),
        ("SELECT lo_export(1, '/tmp/x')", "lo_export"),
        ("SELECT schema_to_xml('auth', true, true, '')", "schema_to_xml"),
        ("SHOW search_path", "not allowed"),
        ("EXPLAIN ANALYZE DELETE FROM accounts WHERE id = 1", "only for reads"),
        ("EXPLAIN SELECT * FROM auth.users", "schema auth"),
        ("COPY accounts TO STDOUT", "not allowed"),
    ],
)
def test_dangerous_statements_are_rejected(sql, reason):
    with pytest.raises(GuardRejection, match=reason):
        inspect_statement(sql)


def test_guard_read_bounds_unlimited_selects_and_keeps_explicit_limits():
    assert guard_read("SELECT id FROM accounts", 500).endswith("LIMIT 501")
    assert guard_read("SELECT id FROM accounts LIMIT 3", 500) == "SELECT id FROM accounts LIMIT 3"
    assert guard_read("EXPLAIN SELECT id FROM accounts", 500) == "EXPLAIN SELECT id FROM accounts"


@pytest.mark.parametrize("sql", ["UPDATE accounts SET name = 'x' WHERE id = 1", "DELETE FROM tags WHERE id = 7",
                                 "INSERT INTO tags (key, value) VALUES ('a', 'b')"])
def test_guard_read_refuses_writes(sql):
    with pytest.raises(GuardRejection, match="read-only on Postgres"):
        guard_read(sql, 500)


def test_redaction_masks_secret_columns_whatever_the_query():
    rows, redacted = redact(["id", "control_secret", "proxy_admin_secret", "state"], [(1, "s3cr3t", None, "warm")])
    assert rows == [[1, "‹redacted›", None, "warm"]]
    assert redacted == ["control_secret", "proxy_admin_secret"]


def test_json_cells_render_as_json_text():
    rows, _ = redact(["payload", "tags"], [({"op": "grip", "force": 0.5}, ["a", "b"])])
    assert rows == [['{"op": "grip", "force": 0.5}', '["a", "b"]']]
