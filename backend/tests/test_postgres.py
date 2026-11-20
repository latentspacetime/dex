"""dex against a real Postgres server (the session cluster from conftest).

The api environment runs the Supabase transport end to end except for the
HTTPS hop: _post is replaced by one that executes the exact request body
(wrapped SQL, $n parameters, read_only) on the local server and answers in
the Management API's shape, a list of row objects from the last statement.
"""

import json
import os
from pathlib import Path

import psycopg
import pytest

from dex import index, schema
from dex.config import INDEX_DIR
from dex.db import QueryError, ReadOnlyViolation, SupabaseApiDatabase, open_database
from dex.graph import Explorer
from dex.overview import OverviewBuilder
from dex.server import Server

ROBOT_ONE = "3f1c2a7e-8b9d-4c5e-a1f2-0123456789ab"


def _local_post(database: SupabaseApiDatabase):
    """The Management API's contract, run locally: one read-only transaction,
    the last statement's rows as JSON objects, errors as QueryError."""
    def post(query: str, parameters: tuple) -> list[dict]:
        assert database.url.endswith("/projects/localtest/database/query")
        with psycopg.connect(os.environ["DEX_TEST_PG_URL"]) as connection:
            connection.read_only = True
            try:
                with psycopg.RawCursor(connection) as cursor:
                    cursor.execute(query, list(parameters) or None)
                    while cursor.nextset():  # the API answers with the last statement's rows
                        pass
                    names = [column.name for column in cursor.description]
                    rows = cursor.fetchall()
            except psycopg.Error as error:
                raise QueryError(error.diag.message_primary or str(error), error.sqlstate or "") from None
        return [dict(zip(names, row, strict=True)) for row in rows]
    return post


@pytest.fixture(params=["test", "api"])
def database(request, postgres_url):
    opened = open_database(request.param)
    if isinstance(opened, SupabaseApiDatabase):
        opened._post = _local_post(opened)
    return opened


@pytest.fixture
def loaded(database):
    return schema.load(database, refresh=True)


def test_reads_run_read_only_and_say_so(database):
    identity = database.server_identity()
    assert identity["read_only"] == "on"
    assert identity["transport"] in ("postgres", "supabase_api")
    # nextval() writes and fits inside the API transport's wrapper subquery.
    with pytest.raises(QueryError) as caught:
        database.read("SELECT nextval('ticket_numbers')")
    assert caught.value.sqlstate == "25006"  # read_only_sql_transaction


def test_a_session_setting_cannot_unlock_later_reads(postgres_url):
    """set_config(..., false) inside a read-only transaction is still allowed;
    each read is rolled back, so it never outlives its own statement."""
    database = open_database("test")
    database.read("SELECT set_config('default_transaction_read_only', 'off', false)")
    with pytest.raises(QueryError, match="read-write mode must be set before any query"):
        database.read("SELECT set_config('transaction_read_only', 'off', false)")
    with pytest.raises(QueryError) as caught:
        database.read("CREATE TABLE escaped (id int)")
    assert caught.value.sqlstate == "25006"


def test_server_identity_refuses_a_writable_session(postgres_url):
    database = open_database("test")
    database.read = lambda sql: type("R", (), {"columns": ["read_only", "standard_strings"], "rows": [["off", "on"]]})()
    with pytest.raises(ReadOnlyViolation):
        database.server_identity()


def test_introspection_sees_public_only_with_types_and_edges(loaded):
    assert "patients" in loaded.tables and "robots" in loaded.tables
    assert "users" not in loaded.tables  # auth.users stays invisible
    assert "api_tokens" not in loaded.tables  # denied by default
    patients = loaded.tables["patients"]
    assert patients.primary_key == ["id"] and patients.column_types["id"] == "text"
    assert patients.soft_delete_column == "deleted_at"
    assert loaded.tables["robots"].column_types["id"] == "uuid"
    assert loaded.tables["caches"].primary_key == ["query_key"]
    edges = {(edge.child, edge.column): (edge.parent, edge.kind) for edge in loaded.edges}
    assert edges[("encounters", "patient_id")] == ("patients", "fk")
    assert edges[("robot_commands", "robot_id")] == ("robots", "implicit")


def test_identifier_lookup_matches_text_uuid_and_integer_ids(database, loaded):
    explorer = Explorer(database, loaded, {})
    # A numeric-looking text id keeps its leading zeros.
    assert [(hit.table, hit.id, hit.label) for hit in explorer.lookup_identifier("00042")] == [("patients", "00042", "Ada Lovelace")]
    assert [(hit.table, hit.id, hit.label) for hit in explorer.lookup_identifier(ROBOT_ONE)] == [("robots", ROBOT_ONE, "Arm One")]
    assert [(hit.table, hit.id) for hit in explorer.lookup_identifier("7")] == [("counters", "7")]
    external = explorer.lookup_identifier("MRN-8")
    assert [(hit.table, hit.id, hit.column) for hit in external] == [("patients", "p-2", "mrn")]
    assert explorer.lookup_identifier("no-such-thing-1") == []


def test_card_shows_parents_children_and_json(database, loaded):
    explorer = Explorer(database, loaded, {})
    patient = explorer.card("patients", "00042")
    assert patient.label == "patients #00042"  # no label column; overrides label hits and parents only
    assert [(group.table, group.via, group.count) for group in patient.children] == [("encounters", "patient_id", 2)]
    robot = explorer.card("robots", ROBOT_ONE)
    assert [(group.table, group.count) for group in robot.children] == [("robot_commands", 1)]
    command_id = robot.children[0].items[0].id
    command = explorer.card("robot_commands", command_id)
    assert [(parent.table, parent.label) for parent in command.parents] == [("robots", "Arm One")]
    payload = command.values[command.columns.index("payload")]
    assert json.loads(payload) == {"op": "grip", "force": 0.5}
    with pytest.raises(LookupError):
        explorer.card("robots", "not-a-uuid")
    with pytest.raises(LookupError):
        explorer.card("patients", "missing")


def test_duplicate_column_names_and_order_survive(database):
    result = database.read('SELECT p.id, e.id, e.reason, p."first_name" FROM patients p JOIN encounters e ON e.patient_id = p.id '
                           "ORDER BY e.id DESC")
    assert result.columns == ["id", "id", "reason", "first_name"]
    assert [row[1] for row in result.rows] == ["e-3", "e-2", "e-1"]
    assert result.rows[0] == ["p-2", "e-3", "migraine", "Grace"]


def test_row_cap_marks_truncation(database):
    result = database.read("SELECT generate_series(1, 20) AS n", row_cap=5)
    assert result.truncated and [row[0] for row in result.rows] == [1, 2, 3, 4, 5]


def test_typed_sql_hits_its_statement_timeout(database, monkeypatch):
    monkeypatch.setattr("dex.db.READ_STATEMENT_TIMEOUT_MS", 200)
    with pytest.raises(QueryError) as caught:
        database.read("SELECT pg_sleep(2)")
    assert caught.value.sqlstate == "57014"


def test_overview_skips_text_dates_and_soft_deleted_rows(database, loaded):
    from concurrent.futures import ThreadPoolExecutor
    built = OverviewBuilder(database, loaded, ThreadPoolExecutor(2), {}).build()
    activity = {entry["label"]: entry for entry in built["activity"]}
    assert "encounters" not in activity  # created_at is text there
    assert activity["patients"]["week"] == 3 and activity["patients"]["day"] == 2
    recent = {group["table"]: [row["id"] for row in group["rows"]] for group in built["recent"]}
    assert "p-3" not in recent.get("patients", [])


def test_memory_index_never_touches_the_disk(postgres_url):
    database = open_database("memory")
    loaded = schema.load(database, refresh=True)
    counts = index.build(database, loaded)
    assert counts["names"] > 0 and counts["deep"] > 0
    assert not Path(INDEX_DIR / "memory").exists()
    assert ("patients", "00042") in {(hit.table, hit.id) for hit in index.fuzzy("lovelace", "memory")}
    deep_hits = index.deep("stairs tightness", "memory")
    assert [(hit.table, hit.id, hit.column) for hit in deep_hits] == [("patients", "00042", "notes")]
    deleted = [hit for hit in index.fuzzy("Removed", "memory") if hit.id == "p-3"]
    assert deleted and deleted[0].deleted


def test_disk_index_writes_both_files(postgres_url):
    database = open_database("test")
    index.build(database, schema.load(database, refresh=True))
    assert Path(index.names_path("test")).stat().st_size > 0
    assert ("robots", ROBOT_ONE) in {(hit.table, hit.id) for hit in index.fuzzy("arm one", "test")}


def test_server_refuses_writes_and_llm_where_disabled(postgres_url):
    server = Server("memory")
    server.initialize()
    assert server.ready.is_set(), server.init_error
    assert server.asker is None
    replies = []
    server.emit = replies.append
    for request in ({"op": "sql", "args": {"sql": "UPDATE counters SET label = 'x' WHERE id = 7"}},
                    {"op": "edit_field", "args": {"table": "counters", "id": "7", "column": "label", "value": "x"}},
                    {"op": "ask", "args": {"question": "how many patients"}},
                    {"op": "route", "args": {"q": "patients with chest pain"}},
                    {"op": "sql", "args": {"sql": "SELECT email FROM auth.users"}}):
        server.handle({"id": len(replies), **request})
    kinds = [reply["error"]["kind"] if not reply["ok"] else reply["result"].get("decided_by") for reply in replies]
    assert kinds == ["write", "write", "guard", "rule", "guard"]
    hello = server.op_hello({})
    assert hello["writes"] is False and hello["environment"] == "memory"
