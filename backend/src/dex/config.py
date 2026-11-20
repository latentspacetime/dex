"""Every machine and database fact dex depends on, loaded from one TOML file.

The file is $DEX_CONFIG, else ~/.config/dex/config.toml. config.example.toml
in the repo documents every key; only [environments.<name>] is required.
"""

import os
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.environ.get("DEX_CONFIG", Path.home() / ".config/dex/config.toml"))
# OPENROUTER_API_KEY is read from the environment first, then from this file
# (KEY=value lines), which keeps the key out of shell startup files.
ENV_FILE = CONFIG_PATH.parent / ".env"

STATE_DIR = Path(os.environ.get("DEX_STATE_DIR", Path.home() / ".local/state/dex"))
INDEX_DIR = STATE_DIR / "index"
ASK_LOG = STATE_DIR / "ask.jsonl"


class ConfigError(Exception):
    """The config file is missing or names something it cannot have."""


class Driver(Enum):
    """How dex reaches an environment's Postgres database."""

    # A direct connection (psycopg) from a URL that url_command prints.
    POSTGRES = "postgres"
    # Supabase's Management API query endpoint (HTTPS). Every request sets
    # read_only, so Supabase runs it as supabase_read_only_user in a read-only
    # transaction; token_command prints the Management API token.
    SUPABASE_API = "supabase_api"


@dataclass(frozen=True)
class Environment:
    name: str
    driver: Driver
    # Prints the connection URL (POSTGRES) or the API token (SUPABASE_API).
    # Its output lives in memory only.
    secret_command: str
    # SUPABASE_API only: the project ref from the dashboard URL.
    project_ref: str
    # Shown in the header; nothing connects by it.
    database: str
    # Shown as a red chip; a non-protected environment shows amber.
    protected: bool
    # False keeps typed input, schema and rows away from every LLM: ASK, the
    # router and summaries are off. Required for data that must not leave
    # this machine (PHI).
    llm_enabled: bool
    # False keeps the search index in the server's memory instead of
    # ~/.local/state/dex, so row labels and text never reach the disk.
    persist_index: bool
    # Run when secret_command fails with an auth marker (e.g. infisical login).
    login_command: str
    auth_markers: tuple[str, ...]


@dataclass(frozen=True)
class Scope:
    """An optional tenant column (brand_id, org_id, account_id) shown on hits."""

    table: str
    column: str
    label: str


@dataclass(frozen=True)
class PolymorphicLink:
    table: str
    type_column: str
    id_column: str
    targets: dict[str, str]  # type value -> table


@dataclass(frozen=True)
class Settings:
    environments: dict[str, Environment]
    default_environment: str
    scope: Scope | None
    label_columns: tuple[str, ...]
    label_overrides: dict[str, str]
    name_skip: frozenset[str]
    deep_sources: tuple[tuple[str, str, int], ...] | None  # None: detect
    external_id_columns: tuple[str, ...]
    priority_tables: tuple[str, ...]
    implicit_references: dict[tuple[str, str], str]
    infer_references: bool
    polymorphic_links: tuple[PolymorphicLink, ...]
    lineage: dict[str, tuple[tuple[tuple[str, str], ...], ...]]
    link_paths: tuple[tuple[str, str], ...]  # (regex with one id group, table)
    link_query: dict[str, str]
    ask_context: str
    domain_tables: frozenset[str]
    home_tables: tuple[str, ...]
    denied_tables: frozenset[str]
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_model: str = ""
    summary_model: str = ""
    router_enabled: bool = True


DEFAULT_AUTH_MARKERS = ("infisical login", "session has expired", "Token has expired", "not logged in")
DEFAULT_LABEL_COLUMNS = (
    "name", "title", "username", "email", "label", "slug", "code", "key", "value", "subject",
    "file_path", "path", "url", "status", "type",
    # Last resort for rows that are their text (comments, messages).
    "content",
)
DEFAULT_DENIED_TABLES = ("auth", "api_tokens", "oauth_tokens", "sessions", "personal_access_tokens")
# The only schema dex introspects, indexes and lets SQL name; Supabase's
# auth, storage and vault schemas and Neon's neon_auth hold credentials.
# pg_catalog and information_schema stay readable for typed introspection.
DATA_SCHEMA = "public"
READABLE_SCHEMAS = frozenset({DATA_SCHEMA, "pg_catalog", "information_schema"})

# Column-name fragments that mark a secret. Matched as substrings of the
# lower-cased column name.
SECRET_COLUMN_FRAGMENTS = (
    "secret",
    "password",
    "_hash",
    "salt",
    "refresh_token",
    "access_token",
    "oauth_token",
    "api_key",
    "access_info",
)
REDACTED = "‹redacted›"

# Server-side statement cap for every read, in milliseconds.
READ_STATEMENT_TIMEOUT_MS = 15_000
# Rows any single read returns to the UI. Ask queries are wrapped to this.
READ_ROW_CAP = 500

INDEX_MAX_AGE_SECONDS = 6 * 3600


SECRET_COMMAND_KEYS = {Driver.POSTGRES: "url_command", Driver.SUPABASE_API: "token_command"}


def _environment(name: str, raw: dict) -> Environment:
    if "allow_writes" in raw or "write" in raw:
        raise ConfigError(f"environments.{name}: dex is read-only on Postgres; remove allow_writes")
    try:
        driver = Driver(raw.get("driver", Driver.POSTGRES.value))
    except ValueError:
        raise ConfigError(f"environments.{name}.driver must be one of {[d.value for d in Driver]}") from None
    secret_key = SECRET_COMMAND_KEYS[driver]
    if not raw.get(secret_key):
        raise ConfigError(f"environments.{name} ({driver.value}) needs {secret_key}")
    if driver is Driver.SUPABASE_API and not raw.get("project_ref"):
        raise ConfigError(f"environments.{name} (supabase_api) needs project_ref")
    return Environment(
        name=name,
        driver=driver,
        secret_command=str(raw[secret_key]),
        project_ref=str(raw.get("project_ref", "")),
        database=str(raw.get("database", name)),
        protected=bool(raw.get("protected", True)),
        llm_enabled=bool(raw.get("llm", True)),
        persist_index=bool(raw.get("persist_index", True)),
        login_command=str(raw.get("login_command", "")),
        auth_markers=tuple(raw.get("auth_markers", DEFAULT_AUTH_MARKERS)),
    )


def _pairs(raw: dict[str, str], what: str) -> dict[tuple[str, str], str]:
    pairs = {}
    for key, target in raw.items():
        table, _, column = key.partition(".")
        if not column:
            raise ConfigError(f"{what} key {key!r} must be table.column")
        pairs[(table, column)] = target
    return pairs


def _load() -> Settings:
    if not CONFIG_PATH.exists():
        raise ConfigError(f"no config at {CONFIG_PATH}; copy config.example.toml there and fill it in")
    with open(CONFIG_PATH, "rb") as handle:
        raw = tomllib.load(handle)
    environments = {name: _environment(name, body) for name, body in (raw.get("environments") or {}).items()}
    if not environments:
        raise ConfigError(f"{CONFIG_PATH} defines no [environments.<name>]")
    default = raw.get("default_environment") or next(iter(environments))
    if default not in environments:
        raise ConfigError(f"default_environment {default!r} is not one of {list(environments)}")

    scope_raw = raw.get("scope")
    scope = Scope(scope_raw["table"], scope_raw["column"], scope_raw.get("label", "name")) if scope_raw else None
    search = raw.get("search", {})
    schema = raw.get("schema", {})
    ask = raw.get("ask", {})
    links = raw.get("links", {})
    deep_raw = search.get("deep_sources")
    return Settings(
        environments=environments,
        default_environment=default,
        scope=scope,
        label_columns=tuple(search.get("label_columns", ())) + DEFAULT_LABEL_COLUMNS,
        label_overrides=dict(search.get("label_overrides", {})),
        name_skip=frozenset(search.get("skip_tables", ())),
        deep_sources=tuple((table, column, int(cap)) for table, column, cap in deep_raw) if deep_raw is not None else None,
        external_id_columns=tuple(search.get("external_id_columns", ())),
        priority_tables=tuple(search.get("priority_tables", ())),
        implicit_references=_pairs(schema.get("references", {}), "schema.references"),
        infer_references=bool(schema.get("infer_references", True)),
        polymorphic_links=tuple(
            PolymorphicLink(link["table"], link["type_column"], link["id_column"], dict(link["targets"]))
            for link in schema.get("polymorphic", ())
        ),
        lineage={
            table: tuple(tuple((step[0], step[1]) for step in path) for path in paths)
            for table, paths in (schema.get("lineage") or {}).items()
        },
        link_paths=tuple((entry["pattern"], entry["table"]) for entry in links.get("paths", ())),
        link_query=dict(links.get("query", {})),
        ask_context=str(ask.get("context", "")).strip(),
        domain_tables=frozenset(ask.get("domain_tables", ())),
        home_tables=tuple((raw.get("home") or {}).get("tables", ())),
        denied_tables=frozenset(DEFAULT_DENIED_TABLES) | frozenset(schema.get("denied_tables", ())),
        models=dict(ask.get("models", {})),
        default_model=str(ask.get("default_model", "")),
        summary_model=str(ask.get("summary_model", "")),
        router_enabled=bool(ask.get("router", True)),
    )


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = _load()
    return _settings


def environment(name: str | None) -> Environment:
    loaded = settings()
    name = name or loaded.default_environment
    try:
        return loaded.environments[name]
    except KeyError:
        raise ConfigError(f"unknown environment {name!r}; expected one of {list(loaded.environments)}") from None


def is_secret_column(column: str) -> bool:
    lowered = column.lower()
    return any(fragment in lowered for fragment in SECRET_COLUMN_FRAGMENTS)


def is_denied_table(table: str) -> bool:
    try:
        return table in settings().denied_tables
    except ConfigError:
        return table in DEFAULT_DENIED_TABLES
