"""dex: fast read-only explorer for a Postgres database.

  dex init                       write a starter config to ~/.config/dex/config.toml
  dex serve                      JSON-lines server for the nvim UI
  dex find <text|id>             id lookup + fuzzy + deep search
  dex card <table> <id>          a row with its parents, lineage, and children
  dex ask "<question>"           natural language → SQL → rows
  dex sql "<statement>"          run one read (writes are refused)
  dex index                      rebuild the local search index
  dex doctor                     check config, connection, read-only, index, LLM key

--env <name> picks an environment from the config (default: default_environment).
"""

import argparse
import json
import shutil
import sys
from importlib import resources

from dex import credentials, index, llm, schema
from dex.ask import DEFAULT_MODEL, MODEL_PROFILES, Asker
from dex.config import CONFIG_PATH, READ_ROW_CAP, ConfigError, environment
from dex.db import Database, open_database
from dex.graph import Explorer
from dex.guard import StatementKind, guard_read, inspect_statement
from dex.server import READ_ONLY_MESSAGE, Server


def _print_table(columns: list[str], rows: list[list]) -> None:
    if not columns:
        print("(no columns)")
        return
    cells = [[("NULL" if value is None else str(value)).replace("\n", " ")[:60] for value in row] for row in rows]
    widths = [max([len(column)] + [len(row[index]) for row in cells]) for index, column in enumerate(columns)]
    print("  ".join(column.ljust(width) for column, width in zip(columns, widths)))
    print("  ".join("─" * width for width in widths))
    for row in cells:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)))
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def _explorer(database: Database) -> Explorer:
    loaded = schema.load(database)
    return Explorer(database, loaded, index.scope_names(database, loaded))


def command_find(arguments, database: Database) -> int:
    explorer = _explorer(database)
    if not database.target.persist_index:
        # This environment's index lives in memory only; a CLI run builds its own.
        index.build(database, explorer.schema)
    hits = []
    if any(character.isdigit() for character in arguments.text) and " " not in arguments.text.strip():
        hits += explorer.lookup_identifier(arguments.text)
    hits += index.fuzzy(arguments.text, database.environment, 20) + index.deep(arguments.text, database.environment, 10)
    for hit in hits:
        detail = f" [{hit.column}]" if hit.column else ""
        print(f"{hit.source:<5} {hit.table:<24} {hit.id:<10} {hit.scope[:18]:<18} {hit.label[:90]}{detail}")
    return 0


def command_card(arguments, database: Database) -> int:
    print(json.dumps(_explorer(database).card(arguments.table, arguments.id).to_json(), indent=2, default=str))
    return 0


def command_ask(arguments, database: Database) -> int:
    if not database.target.llm_enabled:
        print(f"ASK is off for {database.environment}: its data must not leave this machine", file=sys.stderr)
        return 2
    loaded = schema.load(database)
    result = Asker(database, loaded, index.scope_names(database, loaded)).ask(arguments.question, arguments.model)
    print(f"-- route {result.route} ({result.route_confidence:.2f}) · {result.model} · {result.llm_ms:.0f} ms · ${result.cost_usd:.4f}")
    if result.sql:
        print(result.sql)
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
        return 1
    _print_table(result.columns, result.rows)
    return 0


def command_sql(arguments, database: Database) -> int:
    statement = inspect_statement(arguments.statement)
    if statement.kind is not StatementKind.READ:
        print(f"{statement.kind.value.upper()}: {READ_ONLY_MESSAGE}", file=sys.stderr)
        return 1
    result = database.read(guard_read(arguments.statement, READ_ROW_CAP))
    _print_table(result.columns, result.rows)
    if result.truncated:
        print(f"(cut at {READ_ROW_CAP} rows)")
    return 0


def command_index(arguments, database: Database) -> int:
    counts = index.build(database, schema.load(database, refresh=True), progress=lambda message: print(f"  {message}", file=sys.stderr))
    print(json.dumps(counts))
    if not database.target.persist_index:
        print(f"{database.environment} keeps its index in memory: this build proved it works, and `dex serve` "
              "builds its own at start", file=sys.stderr)
    return 0


def command_doctor(arguments, database: Database) -> int:
    checks = []
    try:
        identity = database.server_identity()  # raises unless reads are read-only
        checks.append(("read connection", True,
                       f"{identity['user']}@{identity['database']} · postgres {identity['version']} · {identity['transport']}"))
        checks.append(("read-only enforced", True, "transaction_read_only = on"))
    except credentials.AuthExpired as error:
        hint = environment(database.environment).login_command or "your login command"
        checks.append(("login", False, f"{error}: run `{hint}`"))
    except Exception as error:
        checks.append(("read connection", False, str(error)[:200]))
    if database.target.persist_index:
        age = index.index_age_seconds(database.environment)
        checks.append(("search index", age is not None and not index.index_is_stale(database.environment),
                       "missing" if age is None else f"{age / 3600:.1f} h old"))
    else:
        checks.append(("search index", True, "in memory only; dex serve builds it at start"))
    if database.target.llm_enabled:
        try:
            llm.api_key()
            checks.append(("openrouter key", True, "found"))
        except llm.LlmError as error:
            checks.append(("openrouter key", False, str(error)))
    else:
        checks.append(("llm", True, "off for this environment (ASK, router, summaries)"))
    for name, passed, detail in checks:
        print(f"{'✓' if passed else '✗'} {name:<20} {detail}")
    return 0 if all(passed for _, passed, _ in checks) else 1


def command_init(arguments) -> int:
    if CONFIG_PATH.exists() and not arguments.force:
        print(f"{CONFIG_PATH} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with resources.as_file(resources.files("dex") / "config.example.toml") as example:
        shutil.copyfile(example, CONFIG_PATH)
    CONFIG_PATH.chmod(0o600)
    print(f"wrote {CONFIG_PATH}; fill in [environments.<name>], then run `dex doctor`")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="dex", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=None, help="environment name from the config")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--force", action="store_true")
    commands.add_parser("serve")
    find = commands.add_parser("find")
    find.add_argument("text")
    card = commands.add_parser("card")
    card.add_argument("table")
    card.add_argument("id")
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--model", choices=list(MODEL_PROFILES), default=DEFAULT_MODEL)
    sql = commands.add_parser("sql")
    sql.add_argument("statement")
    commands.add_parser("index")
    commands.add_parser("doctor")
    arguments = parser.parse_args()

    if arguments.command == "init":
        return command_init(arguments)
    if arguments.command == "serve":
        Server(arguments.env).serve()
        return 0
    handlers = {
        "find": command_find, "card": command_card, "ask": command_ask, "sql": command_sql,
        "index": command_index, "doctor": command_doctor,
    }
    try:
        name = environment(arguments.env).name
        return handlers[arguments.command](arguments, open_database(name))
    except ConfigError as error:
        print(f"config: {error}", file=sys.stderr)
        return 2
    except credentials.AuthExpired as error:
        hint = environment(arguments.env).login_command
        print(f"{error}." + (f" Run: {hint}" if hint else ""), file=sys.stderr)
        return 3
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
