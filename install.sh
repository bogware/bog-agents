#!/bin/sh
# Install bog-agents-cli on macOS / Linux (ROADMAP #61).
#
#   curl -LsSf https://raw.githubusercontent.com/bogware/bog-agents/main/install.sh | sh
#   sh install.sh --extras anthropic --method uv
#
# Picks `uv tool` (isolated, brings its own Python when none is installed),
# then `pipx`, then `pip --user`; refreshes PATH hints; runs `bog-agents --doctor`.
# Idempotent: re-running upgrades to the requested version.
#
# Options:
#   --version X.Y.Z     exact bog-agents-cli version (default: latest)
#   --extras NAME       provider extras, e.g. anthropic | openai | bedrock | all-providers
#   --method M          auto | uv | pipx | pip   (default: auto)
#   --no-doctor         skip the final `bog-agents --doctor`
set -eu

VERSION=""
EXTRAS=""
METHOD="auto"
DOCTOR=1
MIN_MAJOR=3
MIN_MINOR=11

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --extras) EXTRAS="$2"; shift 2 ;;
    --method) METHOD="$2"; shift 2 ;;
    --no-doctor) DOCTOR=0; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$1" >&2; }

SPEC="bog-agents-cli"
[ -n "$EXTRAS" ] && SPEC="bog-agents-cli[$EXTRAS]"
[ -n "$VERSION" ] && SPEC="$SPEC==$VERSION"

find_python() {
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      ver=$("$candidate" -c 'import sys; print("%d %d" % sys.version_info[:2])' 2>/dev/null) || continue
      major=${ver% *}; minor=${ver#* }
      if [ "$major" -gt "$MIN_MAJOR" ] || { [ "$major" -eq "$MIN_MAJOR" ] && [ "$minor" -ge "$MIN_MINOR" ]; }; then
        echo "$candidate"; return 0
      fi
      warn "$candidate is Python $major.$minor; bog-agents needs $MIN_MAJOR.$MIN_MINOR+"
    fi
  done
  return 1
}

PYTHON=$(find_python || true)
HAVE_UV=$(command -v uv 2>/dev/null || true)
HAVE_PIPX=$(command -v pipx 2>/dev/null || true)

CHOSEN="$METHOD"
if [ "$CHOSEN" = "auto" ]; then
  if [ -n "$HAVE_UV" ]; then CHOSEN=uv
  elif [ -n "$HAVE_PIPX" ]; then CHOSEN=pipx
  elif [ -n "$PYTHON" ]; then CHOSEN=pip
  else CHOSEN=uv; fi
fi

step "Installing $SPEC via $CHOSEN"
case "$CHOSEN" in
  uv)
    if [ -z "$HAVE_UV" ]; then
      step "uv not found; installing it (https://astral.sh/uv)"
      curl -LsSf https://astral.sh/uv/install.sh | sh
      export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
      command -v uv >/dev/null 2>&1 || { echo "uv did not land on PATH; open a new shell and re-run, or use --method pip" >&2; exit 1; }
    fi
    if [ -n "$PYTHON" ]; then uv tool install --force "$SPEC"; else uv tool install --force --python 3.12 "$SPEC"; fi
    uv tool update-shell >/dev/null 2>&1 || true
    export PATH="$HOME/.local/bin:$PATH"
    ;;
  pipx)
    if [ -z "$HAVE_PIPX" ]; then
      [ -n "$PYTHON" ] || { echo "pipx and Python are both missing; use --method uv" >&2; exit 1; }
      step "pipx not found; installing it for the current user"
      "$PYTHON" -m pip install --user pipx
      "$PYTHON" -m pipx ensurepath >/dev/null 2>&1 || true
      export PATH="$HOME/.local/bin:$PATH"
      HAVE_PIPX="$PYTHON -m pipx"
    fi
    $HAVE_PIPX install --force "$SPEC"
    export PATH="$HOME/.local/bin:$PATH"
    ;;
  pip)
    [ -n "$PYTHON" ] || { echo "No Python $MIN_MAJOR.$MIN_MINOR+ found; install one or use --method uv" >&2; exit 1; }
    "$PYTHON" -m pip install --user --upgrade "$SPEC"
    SCRIPTS=$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_path("scripts", f"{__import__("os").name}_user"))' 2>/dev/null || true)
    [ -n "$SCRIPTS" ] && export PATH="$SCRIPTS:$PATH"
    ;;
  *) echo "unknown --method $CHOSEN" >&2; exit 2 ;;
esac

if ! command -v bog-agents >/dev/null 2>&1; then
  warn "bog-agents is installed but not on PATH in this shell; add ~/.local/bin to PATH or open a new shell."
  exit 0
fi
step "Installed: $(bog-agents --version 2>&1 | head -n 1)"
if [ "$DOCTOR" -eq 1 ]; then
  step "bog-agents --doctor"
  bog-agents --doctor || true
fi
printf '\n\033[32mNext: export a provider key (e.g. ANTHROPIC_API_KEY) and run `bog-agents`.\033[0m\n'
