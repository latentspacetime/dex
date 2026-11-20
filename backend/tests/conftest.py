"""Point dex at a test config before any dex module reads it, and boot a
throwaway Postgres cluster for the tests that need a real server.

Environments (all the same local database):
  test    direct, index on disk
  memory  direct, index in memory only, LLM off (the PHI shape)
  api     the Supabase Management API transport, with its HTTP call replaced
          by one that runs the request's SQL on the local server
"""

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

TEST_CONFIG = """
default_environment = "test"

[environments.test]
url_command = "printf %s \\"$DEX_TEST_PG_URL\\""
protected = false

[environments.memory]
url_command = "printf %s \\"$DEX_TEST_PG_URL\\""
llm = false
persist_index = false

[environments.api]
driver = "supabase_api"
project_ref = "localtest"
token_command = "printf test-token"
llm = false
persist_index = false

[search]
label_overrides = { patients = "concat_ws(' ', first_name, last_name)" }
external_id_columns = ["mrn"]

[[links.paths]]
pattern = "orders/([\\\\w-]+)"
table = "orders"

[[links.paths]]
pattern = "customers/([\\\\w-]+)"
table = "customers"

[links.query]
product = "products"
"""

_directory = Path(tempfile.mkdtemp(prefix="dex-test-"))
(_directory / "config.toml").write_text(TEST_CONFIG)
os.environ["DEX_CONFIG"] = str(_directory / "config.toml")
os.environ["DEX_STATE_DIR"] = str(_directory / "state")

FIXTURE_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA auth;
CREATE TABLE auth.users (id uuid PRIMARY KEY, email text, encrypted_password text);
INSERT INTO auth.users VALUES (gen_random_uuid(), 'root@example.com', 'x');

CREATE TABLE patients (
    id text PRIMARY KEY, first_name text, last_name text, mrn text, notes text,
    created_at timestamptz NOT NULL, deleted_at timestamptz
);
CREATE TABLE encounters (
    id text PRIMARY KEY, patient_id text REFERENCES patients(id), reason text,
    created_at text  -- a date kept as text, as some real schemas do
);
CREATE TABLE robots (id uuid PRIMARY KEY, name text, created_at timestamptz NOT NULL);
CREATE TABLE robot_commands (
    id uuid PRIMARY KEY, robot_id uuid, payload jsonb, created_at timestamptz NOT NULL
);
CREATE TABLE counters (id bigint PRIMARY KEY, label text, "order" text);
CREATE TABLE caches (query_key text PRIMARY KEY, value text);
CREATE SEQUENCE ticket_numbers;
CREATE TABLE api_tokens (id bigint PRIMARY KEY, token text);

INSERT INTO patients VALUES
  ('00042', 'Ada', 'Lovelace', 'MRN-7', 'complains of intermittent chest tightness after stairs', now() - interval '2 hours', NULL),
  ('p-2', 'Grace', 'Hopper', 'MRN-8', 'follow-up for migraine', now() - interval '3 days', NULL),
  ('p-3', 'Removed', 'Person', 'MRN-9', 'deleted row', now() - interval '1 hour', now());
INSERT INTO encounters VALUES
  ('e-1', '00042', 'chest pain', '2026-10-01'), ('e-2', '00042', 'stairs', '2026-10-02'), ('e-3', 'p-2', 'migraine', '2026-10-03');
INSERT INTO robots VALUES
  ('3f1c2a7e-8b9d-4c5e-a1f2-0123456789ab', 'Arm One', now() - interval '1 day'),
  ('9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d', 'Arm Two', now() - interval '5 days');
INSERT INTO robot_commands VALUES
  (gen_random_uuid(), '3f1c2a7e-8b9d-4c5e-a1f2-0123456789ab', '{"op": "grip", "force": 0.5}', now());
INSERT INTO counters VALUES (7, 'seven', 'first'), (8, 'eight', 'second');
INSERT INTO caches VALUES ('k', 'v');
INSERT INTO api_tokens VALUES (1, 'secret');
ANALYZE;
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="session")
def postgres_url():
    if not shutil.which("initdb") or not shutil.which("pg_ctl"):
        pytest.skip("needs a local Postgres (initdb, pg_ctl) for real-server tests")
    data = _directory / "pgdata"
    port = _free_port()
    subprocess.run(["initdb", "-D", str(data), "-U", "dex", "--auth=trust", "--no-sync"], check=True, capture_output=True)
    subprocess.run(["pg_ctl", "-D", str(data), "-o", f"-p {port} -k {_directory} -c listen_addresses=''",
                    "-l", str(_directory / "pg.log"), "-w", "start"], check=True, capture_output=True)
    url = f"postgresql://dex@/postgres?host={_directory}&port={port}"
    import psycopg
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(FIXTURE_SQL)
    os.environ["DEX_TEST_PG_URL"] = url
    yield url
    subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
