#!/usr/bin/env bash
# Rebrand: hugo -> bogtown, hugo_cli -> bog_agents_cli, hugo-cli -> bog-agents-cli, etc.
# This script does content replacements in all source files.
set -euo pipefail

cd "$(dirname "$0")/.."

# Exclude directories
EXCLUDE="--exclude-dir=.venv --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=node_modules --exclude-dir=dist --exclude-dir=*.egg-info"

# File types to process
INCLUDE="--include=*.py --include=*.toml --include=*.md --include=*.yml --include=*.yaml --include=*.json --include=*.sh --include=*.ts --include=*.js --include=*.tcss --include=*.cfg --include=*.txt"

echo "=== Step 1: Content replacements ==="

# Order matters — do longer/more specific patterns first to avoid double-replacement

# 1. Python package imports and module names (hugo_cli -> bog_agents_cli, hugo_harbor -> bog_agents_harbor, hugo_acp -> bog_agents_acp)
find . -type f \( -name "*.py" -o -name "*.toml" -o -name "*.md" -o -name "*.yml" -o -name "*.yaml" -o -name "*.json" -o -name "*.sh" -o -name "*.ts" -o -name "*.js" -o -name "*.tcss" -o -name "*.txt" \) \
  ! -path "*/.venv/*" ! -path "*/.git/*" ! -path "*/__pycache__/*" ! -path "*/dist/*" ! -path "*/*.egg-info/*" ! -path "*/node_modules/*" ! -path "*/scripts/rebrand.sh" \
  -exec sed -i \
    -e 's/hugo_harbor/bog_agents_harbor/g' \
    -e 's/hugo_acp/bog_agents_acp/g' \
    -e 's/hugo_cli/bog_agents_cli/g' \
    -e 's/hugo-harbor/bog-agents-harbor/g' \
    -e 's/hugo-acp/bog-agents-acp/g' \
    -e 's/hugo-cli/bog-agents-cli/g' \
    -e 's/hugo-vscode/bog-agents-vscode/g' \
    -e 's/hugo\.js/bogtown\.js/g' \
    {} +

# 2. Replace remaining "hugo" references (package name, branding, etc.)
# Be careful with case sensitivity — Hugo (capitalized) is the brand name
find . -type f \( -name "*.py" -o -name "*.toml" -o -name "*.md" -o -name "*.yml" -o -name "*.yaml" -o -name "*.json" -o -name "*.sh" -o -name "*.ts" -o -name "*.js" -o -name "*.tcss" -o -name "*.txt" \) \
  ! -path "*/.venv/*" ! -path "*/.git/*" ! -path "*/__pycache__/*" ! -path "*/dist/*" ! -path "*/*.egg-info/*" ! -path "*/node_modules/*" ! -path "*/scripts/rebrand.sh" \
  -exec sed -i \
    -e 's/\"hugo\"/\"bogtown\"/g' \
    -e "s/'hugo'/'bog-agents'/g" \
    -e 's/name = "hugo"/name = "bog-agents"/g' \
    -e 's/pip install hugo/pip install bog-agents/g' \
    -e 's/uv add hugo/uv add bog-agents/g' \
    -e 's/from hugo /from bog_agents /g' \
    -e 's/from hugo\./from bogtown\./g' \
    -e 's/import hugo/import bog_agents/g' \
    -e 's/hugo\./bogtown\./g' \
    -e 's|libs/hugo/|libs/bog-agents/|g' \
    -e 's|libs/hugo |libs/bog-agents |g' \
    {} +

# 3. Replace Hugo (capitalized brand name) — but NOT in contexts like "Victor Hugo"
find . -type f \( -name "*.py" -o -name "*.toml" -o -name "*.md" -o -name "*.yml" -o -name "*.yaml" -o -name "*.json" -o -name "*.sh" -o -name "*.ts" -o -name "*.js" -o -name "*.tcss" -o -name "*.txt" \) \
  ! -path "*/.venv/*" ! -path "*/.git/*" ! -path "*/__pycache__/*" ! -path "*/dist/*" ! -path "*/*.egg-info/*" ! -path "*/node_modules/*" ! -path "*/scripts/rebrand.sh" \
  -exec sed -i \
    -e 's/Hugo CLI/Bog Agents CLI/g' \
    -e 's/Hugo Agent/Bog Agents Agent/g' \
    -e 's/Hugo agent/Bog Agents agent/g' \
    -e 's/Hugo SDK/Bog Agents SDK/g' \
    -e 's/Hugo Harbor/Bog Agents Harbor/g' \
    -e 's/Hugo ACP/Bog Agents ACP/g' \
    -e 's/Hugo Logo/Bog Agents Logo/g' \
    -e 's/Hugo -/Bog Agents -/g' \
    -e 's/Hugo is/Bog Agents is/g' \
    -e 's/Hugo follows/Bog Agents follows/g' \
    -e 's/Hugo come/Bog Agents come/g' \
    -e 's/Hugo AI/Bog Agents AI/g' \
    -e 's/for Hugo/for Bog Agents/g' \
    -e 's/to Hugo/to Bog Agents/g' \
    -e 's/of Hugo/of Bog Agents/g' \
    -e 's/with Hugo/with Bog Agents/g' \
    -e 's/the Hugo/the Bog Agents/g' \
    -e 's/# Hugo/# Bog Agents/g' \
    -e 's/## Hugo/## Bog Agents/g' \
    -e 's/### Hugo/### Bog Agents/g' \
    -e 's/"Hugo/"Bog Agents/g' \
    -e "s/'Hugo/'Bog Agents/g" \
    -e 's/Hugo\./Bog Agents\./g' \
    -e 's/Hugo:/Bog Agents:/g' \
    -e 's/Hugo"/Bog Agents"/g' \
    -e "s/Hugo'/Bog Agents'/g" \
    -e 's/Hugo,/Bog Agents,/g' \
    -e 's/ Hugo / Bog Agents /g' \
    {} +

# 4. URLs: hugo -> bog-agents in GitHub URLs and PyPI
find . -type f \( -name "*.py" -o -name "*.toml" -o -name "*.md" -o -name "*.yml" -o -name "*.yaml" -o -name "*.json" -o -name "*.sh" -o -name "*.ts" -o -name "*.js" -o -name "*.tcss" -o -name "*.txt" \) \
  ! -path "*/.venv/*" ! -path "*/.git/*" ! -path "*/__pycache__/*" ! -path "*/dist/*" ! -path "*/*.egg-info/*" ! -path "*/node_modules/*" ! -path "*/scripts/rebrand.sh" \
  -exec sed -i \
    -e 's|langchain-ai/hugo|bogware/bog-agents|g' \
    -e 's|python/hugo|python/bog-agents|g' \
    -e 's|pypi/hugo|pypi/bog-agents|g' \
    -e 's|packages/hugo|packages/bog-agents|g' \
    -e 's|pypi.org/project/hugo|pypi.org/project/bog-agents|g' \
    {} +

# 5. Config directory: .hugo -> .bog-agents
find . -type f \( -name "*.py" -o -name "*.toml" -o -name "*.md" -o -name "*.yml" -o -name "*.yaml" -o -name "*.json" -o -name "*.sh" -o -name "*.ts" -o -name "*.js" -o -name "*.tcss" -o -name "*.txt" \) \
  ! -path "*/.venv/*" ! -path "*/.git/*" ! -path "*/__pycache__/*" ! -path "*/dist/*" ! -path "*/*.egg-info/*" ! -path "*/node_modules/*" ! -path "*/scripts/rebrand.sh" \
  -exec sed -i \
    -e 's/\.hugo/\.bog-agents/g' \
    {} +

# 6. Environment variable prefixes: HUGO_ -> BOG_AGENTS_
find . -type f \( -name "*.py" -o -name "*.toml" -o -name "*.md" -o -name "*.yml" -o -name "*.yaml" -o -name "*.json" -o -name "*.sh" -o -name "*.ts" -o -name "*.js" -o -name "*.tcss" -o -name "*.txt" \) \
  ! -path "*/.venv/*" ! -path "*/.git/*" ! -path "*/__pycache__/*" ! -path "*/dist/*" ! -path "*/*.egg-info/*" ! -path "*/node_modules/*" ! -path "*/scripts/rebrand.sh" \
  -exec sed -i \
    -e 's/HUGO_EXTRAS/BOG_AGENTS_EXTRAS/g' \
    -e 's/HUGO_PYTHON/BOG_AGENTS_PYTHON/g' \
    -e 's/HUGO_/BOG_AGENTS_/g' \
    {} +

echo "=== Step 2: Rename directories ==="

# Rename Python package directories (hugo_* -> bogtown_*)
[ -d "libs/acp/hugo_acp" ] && mv libs/acp/hugo_acp libs/acp/bog_agents_acp
[ -d "libs/cli/hugo_cli" ] && mv libs/cli/hugo_cli libs/cli/bog_agents_cli
[ -d "libs/harbor/hugo_harbor" ] && mv libs/harbor/hugo_harbor libs/harbor/bog_agents_harbor

# Rename the SDK package directory
[ -d "libs/hugo/hugo" ] && mv libs/hugo/hugo libs/hugo/bogtown

# Rename the SDK lib directory itself
[ -d "libs/hugo" ] && mv libs/hugo libs/bogtown

echo "=== Step 3: Rename workflow files ==="
[ -f ".github/workflows/hugo-example.yml" ] && mv .github/workflows/hugo-example.yml .github/workflows/bog-agents-example.yml

echo "=== Done! ==="
echo "Manual steps remaining:"
echo "  1. Verify SVG logos in .github/images/ display correctly"
echo "  2. Check for any remaining 'hugo' references: grep -ri hugo --include='*.py' --exclude-dir=.venv"
