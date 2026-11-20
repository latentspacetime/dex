# DEX: a read-only explorer for Postgres

**2304: October 19, 2026 PDT**

> **INTENT:** DEX finds rows in a Postgres database from any fragment you have, such as an id, a UUID, a name, a pasted app URL, or words from a note. It shows each row as a card with its fields, the rows it points to, and the rows that point to it. It also runs SQL that you type and answers plain-English questions with SQL. DEX never writes, and each read runs in a read-only transaction at the server. DEX reaches a database in one of two ways: a direct Postgres connection, or Supabase's Management API query endpoint, which needs no database password. You can mark an environment that holds sensitive data (such as PHI) so that nothing from it reaches an LLM and its search index stays in memory. You use DEX from Neovim with `<Leader>d` or from the `dex` command.

## Install

```bash
uv tool install --editable --python 3.12 ./backend   # the dex command, in ~/.local/bin
```

The Neovim UI is the `nvim/` folder. With lazy.nvim, add it as a local plugin:

```lua
{ dir = "/path/to/dex/nvim" },
```

`nvim/plugin/dex.lua` maps `<Leader>d` and `<Leader>D` when nothing else has them, and always defines `:Dex`. Search needs `fzf` and `rg` on the `PATH`.

## Configure

The config is `~/.config/dex/config.toml` (or `$DEX_CONFIG`). Each `[environments.<name>]` block is one database, and `^t` in the Neovim float moves to the next one in file order. `src/dex/config.example.toml` documents every key.

```toml
default_environment = "app"

[environments.app]
driver = "postgres"
url_command = "infisical secrets get DATABASE_URL --plain --silent"   # prints a postgres:// URL
database = "app"                                                      # shown in the header

[environments.clinic]
driver = "supabase_api"
project_ref = "abcdefghijklmnop"
token_command = "infisical secrets get SUPABASE_ACCESS_TOKEN --plain --silent"
llm = false              # no ASK, router, or summaries: typed text and rows stay on this machine
persist_index = false    # search index lives in the server's memory only
```

Then run `dex --env <name> doctor`. It shows the user that reads run as, whether reads are read-only, and the index state.

Secrets come only from the command, and DEX keeps the output in memory. When the command fails with an expired login, DEX runs `login_command` if one is set.

## Safety

- Every read runs in a read-only transaction. With `driver = "postgres"` it is `BEGIN READ ONLY`, followed by a rollback, so a session setting changed by a statement is gone after that statement. With `driver = "supabase_api"` every request sets `read_only`, and Supabase runs it as `supabase_read_only_user`. DEX refuses to connect when the server does not report `transaction_read_only = on`.
- The SQL guard allows one `SELECT`, `WITH`, or `EXPLAIN` statement at a time, capped at 500 rows and 15 seconds. It rejects any schema other than `public`, `pg_catalog`, and `information_schema`, which keeps Supabase's `auth`, `storage`, and `vault` schemas and Neon's `neon_auth` out of reach. It also rejects functions that act outside the query, such as `set_config`, `query_to_xml`, `pg_read_file`, `pg_terminate_backend`, `dblink`, and the `lo_*` family.
- Credential tables (`auth`, `api_tokens`, `sessions`, and so on, plus `[schema].denied_tables`) are never queried. Columns with names like secrets are redacted.
- Writes are refused everywhere. The edit key in a card and typed `UPDATE`, `DELETE`, and `INSERT` statements answer with "dex is read-only on Postgres".

## Use

| Want | Do |
|---|---|
| What is `00042` or a UUID? | type it to find every table where it is an id or an external id |
| A row by name | type part of it, and names are fuzzy-matched |
| Text inside notes or messages | press `Tab` to DEEP and type words in any order |
| A question | press `Tab` to ASK ✦, type it, and press Enter (off where `llm = false`) |
| Raw SQL | press `Tab` to SQL |
| Another environment | `^t` |
| All keys | `Esc`, then `?` |

CLI: `dex find <text>`, `dex card <table> <id>`, `dex ask "…"`, `dex sql "…"`, `dex index`, `dex doctor`, plus `--env <name>`.

## Transport details

The Management API returns rows as JSON objects keyed by column name, which drops duplicate column names and loses column order. DEX wraps each query as `SELECT json_agg(...)::text FROM (<query>) ...` and parses that text, so `SELECT a.id, b.id` keeps both columns in order. An empty result has no column names over this transport. A request with bound parameters can carry only one statement, so DEX's own parameterized queries run under Supabase's server timeout (2 minutes) and a 30 second client timeout, while typed SQL gets the 15 second `statement_timeout`. At most 4 API requests run at once.

## Tests

```bash
cd backend && uv run pytest
```

The real-server tests start a throwaway Postgres cluster with `initdb` and `pg_ctl` and are skipped when those are missing. The `api` test environment runs the Supabase transport against that same server with only the HTTPS call replaced.

## Uninstall

```bash
uv tool uninstall dex
rm -rf ~/.local/state/dex ~/.config/dex
```
