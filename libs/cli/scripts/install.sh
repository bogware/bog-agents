#!/usr/bin/env bash
# Install bog-agents-cli via uv.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/langchain-ai/bog-agents/main/libs/cli/scripts/install.sh | bash
#
# Environment variables:
#   BOG_AGENTS_EXTRAS  — comma-separated pip extras, e.g. "anthropic" or "anthropic,groq"
#                        (see pyproject.toml for available extras)
#   BOG_AGENTS_PYTHON  — Python version to use (default: 3.13)
#   UV_BIN             — path to uv binary (auto-detected if unset)
set -euo pipefail

EXTRAS="${BOG_AGENTS_EXTRAS:-}"
PYTHON_VERSION="${BOG_AGENTS_PYTHON:-3.13}"

# Validate and normalize extras: accept bare CSV, wrap in brackets for pip
if [[ -n "$EXTRAS" ]]; then
  # Strip brackets if the user passed them anyway
  EXTRAS="${EXTRAS#[}"
  EXTRAS="${EXTRAS%]}"
  if [[ ! "$EXTRAS" =~ ^[-a-zA-Z0-9,]+$ ]]; then
    echo "Error: BOG_AGENTS_EXTRAS must be comma-separated extra names, e.g. 'anthropic,groq'" >&2
    exit 1
  fi
  EXTRAS="[${EXTRAS}]"
fi

install_uv() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "Error: curl or wget is required to install uv." >&2
    exit 1
  fi
}

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found — installing..." >&2
  install_uv
fi

# Resolve uv binary: honor UV_BIN override, then PATH, then the default
# install location (~/.local/bin). A fresh install may not have updated PATH
# in the current session, so we source the env file the installer creates.
if [ -z "${UV_BIN:-}" ]; then
  UV_BIN="uv"
  if ! command -v "$UV_BIN" >/dev/null 2>&1; then
    if [ -f "${HOME}/.local/bin/env" ]; then
      # shellcheck source=/dev/null
      . "${HOME}/.local/bin/env"
    fi
  fi
  if ! command -v uv >/dev/null 2>&1; then
    UV_BIN="${HOME}/.local/bin/uv"
    if [ ! -x "$UV_BIN" ]; then
      echo "Error: uv not found after installation. Restart your shell or add ~/.local/bin to PATH." >&2
      exit 1
    fi
  fi
fi

PACKAGE="bog-agents-cli${EXTRAS}"
echo "Installing ${PACKAGE}..." >&2
"$UV_BIN" tool install -U --python "$PYTHON_VERSION" "$PACKAGE"

echo ""
echo "bog-agents-cli installed successfully."
echo "Run:  bog-agents"
echo ""
echo "If the command is not found, restart your shell or run:"
echo "  source ~/.zshrc   # (or ~/.bashrc)"
echo ""
echo "For help and support, see the Bog Agents CLI docs:"
echo "  https://docs.langchain.com/oss/python/bog-agents/cli/overview"
