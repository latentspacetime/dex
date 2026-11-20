#!/usr/bin/env bash
# Install DEX: the `dex` command and a starter config. The Neovim UI loads
# from this folder as a local plugin (see the message at the end).
# Safe to re-run; it updates in place and never overwrites your config.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEX_DIR="$HOME/.config/dex"
missing=()

say() { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }

# ── prerequisites ───────────────────────────────────────────────────────
need() {
    local command="$1" brew="$2" hint="$3"
    command -v "$command" >/dev/null 2>&1 && return
    if command -v brew >/dev/null 2>&1; then
        say "installing $command (brew install $brew)"
        brew install "$brew" && return
    fi
    missing+=("$command — $hint")
}

need uv uv "https://docs.astral.sh/uv/getting-started/installation/"
need fzf fzf "https://github.com/junegunn/fzf#installation"
need rg ripgrep "https://github.com/BurntSushi/ripgrep#installation"

if [ ${#missing[@]} -gt 0 ]; then
    warn "install these first, then re-run ./install.sh:"
    printf '    %s\n' "${missing[@]}"
    exit 1
fi

# ── backend ─────────────────────────────────────────────────────────────
say "installing the dex command (uv tool install)"
uv tool install --force --editable --python 3.12 "$HERE/backend"
uv tool update-shell >/dev/null 2>&1 || true
DEX="$(uv tool dir --bin)/dex"

# ── config ──────────────────────────────────────────────────────────────
if [ -f "$DEX_DIR/config.toml" ]; then
    say "keeping your config at $DEX_DIR/config.toml"
else
    "$DEX" init
fi

# ── OpenRouter key (ASK mode only; search, cards and SQL work without it) ──
if [ -z "${OPENROUTER_API_KEY:-}" ] && ! grep -q "OPENROUTER_API_KEY" "$DEX_DIR/.env" 2>/dev/null && [ -t 0 ]; then
    printf 'OpenRouter API key for ASK mode (Enter to skip): '
    read -rs key; echo
    if [ -n "$key" ]; then
        mkdir -p "$DEX_DIR"
        printf 'OPENROUTER_API_KEY=%s\n' "$key" >>"$DEX_DIR/.env"
        chmod 600 "$DEX_DIR/.env"
        say "saved the key to $DEX_DIR/.env"
    fi
fi

cat <<EOF

Done. Next:
  1. Edit $DEX_DIR/config.toml: point [environments.<name>] at your database.
  2. dex doctor        # checks the connection, read-only mode, index, and key
  3. Add the UI to lazy.nvim:  { dir = "$HERE/nvim" },
  4. nvim, then <Leader>d (or :Dex)
EOF
