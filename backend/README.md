# dex (Python package)

**2304: October 19, 2026 PDT**

> **INTENT:** This folder is the `dex` command and the JSON-lines server behind the Neovim UI. It reads Postgres directly with psycopg, or through Supabase's Management API query endpoint, and never writes. Setup and configuration are in `../INSTALL.md`, and `src/dex/config.example.toml` documents every config key.

Tests: `uv run pytest`. The real-server tests start a throwaway Postgres cluster with `initdb` and `pg_ctl` (Homebrew `postgresql`) and are skipped when those are missing.
