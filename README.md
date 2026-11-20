# DEX

A fast, read-only database explorer for Postgres — available as a CLI (`dex`) and a Neovim floating window (`<Leader>d` / `:Dex`).

DEX finds rows in a Postgres database from any fragment you have: an integer or text ID, a UUID, an external ID, a fuzzy name, a pasted application URL, or words buried inside notes and descriptions. It renders each row as a card showing its fields, the rows it points to, its lineage, and the rows that point to it. It also runs guarded read-only SQL and can optionally answer plain-English questions via OpenRouter.

DEX never writes. Every read runs inside a server-enforced read-only transaction, and DEX connects either directly over Postgres (`psycopg`) or through Supabase's Management API query endpoint (requiring no database password). Sensitive environments (such as PHI) can be marked `llm = false` and `persist_index = false` so no row data leaves the machine or touches disk.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- `fzf` and `rg` (ripgrep) on `PATH` for fuzzy and deep search
- Neovim 0.10+ (for the floating UI; the `dex` CLI works standalone)
- An [OpenRouter](https://openrouter.ai) API key (optional; only needed for `ASK` mode)

## Install

### Quick install (recommended)

```bash
git clone https://github.com/latentspacetime/dex.git
cd dex
./install.sh
```

`install.sh` checks prerequisites (`uv`, `fzf`, `rg`), installs the `dex` CLI with `uv tool install`, and writes a starter config to `~/.config/dex/config.toml` without overwriting an existing one.

### Manual install

Install the `dex` CLI directly from GitHub or a local clone:

```bash
uv tool install "git+https://github.com/latentspacetime/dex.git#subdirectory=backend"
# or from a local clone:
uv tool install --editable ./backend
```

Add the Neovim UI with `lazy.nvim`:

```lua
-- From GitHub:
{
  "latentspacetime/dex",
  lazy = false,
  config = function(plugin)
    vim.opt.rtp:append(plugin.dir .. "/nvim")
    vim.cmd.runtime("plugin/dex.lua")
  end,
}

-- Or from a local clone:
-- { dir = "/path/to/dex/nvim" }
```

`nvim/plugin/dex.lua` maps `<Leader>d` and `<Leader>D` in normal mode when those keys are unbound, and always defines `:Dex`.

## Configure

Configuration lives at `~/.config/dex/config.toml` (override with `$DEX_CONFIG`). Run `dex init` to generate a starter file. Only `[environments.<name>]` is required; [backend/src/dex/config.example.toml](backend/src/dex/config.example.toml) documents every optional section (`scope`, `search`, `schema`, `links`, `ask`, `home`).

```toml
default_environment = "app"

[environments.app]
driver = "postgres"
url_command = "printf %s postgresql://localhost/app_development"
database = "app"
protected = false

[environments.clinic]
driver = "supabase_api"
project_ref = "abcdefghijklmnop"
token_command = "infisical secrets get SUPABASE_ACCESS_TOKEN --plain --silent"
llm = false              # disables ASK, router, and summaries: typed text and rows stay local
persist_index = false    # keeps the search index in server memory only
```

Verify the setup:

```bash
dex doctor
dex --env clinic doctor
```

Secrets are read only from `url_command` or `token_command` and kept in memory. If a secret command fails with an expired login marker, DEX runs `login_command` once if configured.

## Usage

### Neovim (`<Leader>d` or `:Dex`)

| Want | Do |
| --- | --- |
| Look up an ID, UUID, or external ID | Type it in `SEARCH` to find every matching table |
| Find a row by name | Type part of the name (fuzzy matched) |
| Jump from a pasted app URL | Paste the URL (configured under `[links]`) |
| Search inside notes, comments, or bodies | Press `Tab` to `DEEP` and type words in any order |
| Ask a plain-English question | Press `Tab` to `ASK ✦`, type the question, press Enter (off when `llm = false`) |
| Run read-only SQL | Press `Tab` to `SQL` |
| Switch environments | Press `Ctrl-t` |
| View all keybindings | Press `Esc`, then `?` |

### CLI

```bash
dex find <text|id>              # exact id, fuzzy name, and deep text search
dex card <table> <id>           # JSON row card with parents, lineage, and children
dex sql "SELECT count(*) FROM orders"
dex ask "top 10 customers by order count"
dex index                       # rebuild the local search index
dex doctor                      # verify config, connection, read-only state, index, and LLM key
```

Pass `--env <name>` to target any configured environment.

## Safety model

1. **Server-enforced read-only transactions:** Direct `postgres` connections wrap every read in `BEGIN READ ONLY` followed by `ROLLBACK`, so session-level state changes do not persist. The `supabase_api` driver passes `read_only: true` on every request so Supabase executes as `supabase_read_only_user`. DEX refuses to connect unless the server reports `transaction_read_only = on`.
2. **SQL guard (`sqlglot`):** Allows only a single `SELECT`, `WITH`, or `EXPLAIN` (for reads) statement at a time, capped at 500 rows and a 15-second statement timeout. Rejects locking reads (`FOR UPDATE`), schemas outside `public`, `pg_catalog`, and `information_schema` (keeping Supabase `auth`, `storage`, `vault`, and Neon `neon_auth` out of reach), and side-effecting functions (`set_config`, `query_to_xml`, `pg_read_file`, `pg_terminate_backend`, `dblink`, `lo_*`, etc.).
3. **Credential protection:** Tables matching `auth`, `api_tokens`, `oauth_tokens`, `sessions`, `personal_access_tokens`, or `[schema].denied_tables` are never queried. Secret-shaped columns (`*password*`, `*secret*`, `*_hash*`, `*token*`, `*api_key*`, etc.) are automatically redacted.
4. **No writes:** All write statements and UI edit actions are refused with `dex is read-only on Postgres`.
5. **Local-only / PHI mode:** Setting `llm = false` disables all outbound model calls (ASK, routing, and summaries), and `persist_index = false` keeps the search index strictly in memory.

## Supabase Management API transport

Supabase's Management API returns rows as JSON objects keyed by column name, which loses column order and collapses duplicate column names. DEX wraps queries as `SELECT json_agg(...)::text FROM (<query>) ...` and decodes that payload so queries like `SELECT a.id, b.id` preserve all columns in order. Concurrency is capped at 4 in-flight requests. See [INSTALL.md](INSTALL.md) for additional transport and uninstall notes.

## Development and tests

```bash
cd backend
uv run pytest
```

The integration tests boot a throwaway local Postgres cluster using `initdb` and `pg_ctl` (automatically skipped if PostgreSQL binaries are not installed) and test both the direct `psycopg` driver and the wrapped `supabase_api` transport against it.

## License

[MIT](LICENSE)
