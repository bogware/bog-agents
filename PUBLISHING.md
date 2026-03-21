# Publishing Bog Agents to PyPI

Complete guide to registering and publishing the `bog-agents` (SDK) and `bog-agents-cli` (CLI) packages on PyPI so users can install them with `pip install bog-agents` and `pip install bog-agents-cli`.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [One-Time PyPI Setup](#one-time-pypi-setup)
3. [Configure Trusted Publishing (Recommended)](#configure-trusted-publishing-recommended)
4. [Alternative: API Token Publishing](#alternative-api-token-publishing)
5. [First Release: Manual](#first-release-manual)
6. [Automated Releases (CI/CD)](#automated-releases-cicd)
7. [Verify Your Package](#verify-your-package)
8. [Test PyPI (Staging)](#test-pypi-staging)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

Install the tools you need locally:

```bash
# Install uv (package manager used by this project)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install build tools
pip install build twine
```

Ensure you have:
- A [PyPI account](https://pypi.org/account/register/)
- A [Test PyPI account](https://test.pypi.org/account/register/) (same email is fine, but separate registration)
- 2FA enabled on both accounts (required by PyPI since 2024)

---

## One-Time PyPI Setup

### 1. Create Your PyPI Account

Go to https://pypi.org/account/register/ and create an account.

- Use a stable email you control long-term
- Enable 2FA immediately (Settings > Account Security)
- If this is an organization project, consider creating an [organization](https://pypi.org/manage/organizations/) on PyPI

### 2. Register the Package Names

Package names on PyPI are first-come, first-served. You need to register both:

- `bog-agents` (the SDK)
- `bog-agents-cli` (the CLI)

The names are registered automatically when you upload the first version. There is no separate "registration" step.

### 3. Enable 2FA and Recovery Codes

PyPI requires 2FA for all package uploads. Go to:
https://pypi.org/manage/account/#two-factor

Save your recovery codes somewhere safe.

---

## Configure Trusted Publishing (Recommended)

Trusted publishing eliminates the need for API tokens. GitHub Actions authenticates directly with PyPI using OpenID Connect (OIDC). This is what our CI/CD uses.

### For Each Package on PyPI:

1. Go to https://pypi.org/manage/project/bog-agents/settings/publishing/ (or `bog-agents-cli`)
2. Click "Add a new pending publisher" (for first upload) or "Add publisher" (for existing packages)
3. Fill in the form:

| Field | Value |
|-------|-------|
| **Owner** | `langchain-ai` (or your GitHub org/user) |
| **Repository** | `bog-agents` |
| **Workflow name** | `release.yml` |
| **Environment** | _(leave blank)_ |

4. Click "Add"

### For Test PyPI (same steps):

1. Go to https://test.pypi.org/manage/project/bog-agents/settings/publishing/
2. Add the same trusted publisher configuration

### How It Works

When the GitHub Actions workflow runs with `id-token: write` permission, it gets a short-lived OIDC token that PyPI verifies against your trusted publisher config. No secrets to rotate, no tokens to leak.

---

## Alternative: API Token Publishing

If you can't use trusted publishing (e.g., publishing from a local machine):

### 1. Create an API Token

1. Go to https://pypi.org/manage/account/token/
2. Create a token scoped to your specific project (not account-wide)
3. Copy the token (starts with `pypi-`)

### 2. Configure the Token

```bash
# Option A: Environment variable
export TWINE_PASSWORD=pypi-your-token-here
export TWINE_USERNAME=__token__

# Option B: ~/.pypirc file
cat > ~/.pypirc << 'EOF'
[pypi]
username = __token__
password = pypi-your-token-here

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-your-test-token-here
EOF
chmod 600 ~/.pypirc
```

### 3. Add to GitHub Secrets (if not using trusted publishing)

Go to your repo Settings > Secrets and variables > Actions:
- `PYPI_API_TOKEN` - your PyPI token
- `TEST_PYPI_API_TOKEN` - your Test PyPI token

---

## First Release: Manual

For the very first upload of each package, you may need to do it manually to register the package name on PyPI.

### Build the SDK

```bash
cd libs/bog-agents

# Build sdist and wheel
uv run --group test python -m build

# Check the package
twine check dist/*

# Upload to Test PyPI first
twine upload --repository testpypi dist/*

# Test the install from Test PyPI
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ bog-agents

# If everything looks good, upload to real PyPI
twine upload dist/*
```

### Build the CLI

```bash
cd libs/cli

# Build
uv run --group test python -m build

# Check
twine check dist/*

# Upload to Test PyPI
twine upload --repository testpypi dist/*

# Test install
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ bog-agents-cli

# Upload to real PyPI
twine upload dist/*
```

### Verify

```bash
# Wait a minute for PyPI to index, then:
pip install bog-agents
python -c "import bog_agents; print(bog_agents.__version__)"

pip install bog-agents-cli
bog-agents --version
```

---

## Automated Releases (CI/CD)

Once the first version is uploaded and trusted publishing is configured, all subsequent releases are automated.

### How the Pipeline Works

```
Commit to main
    │
    ▼
release-please analyzes conventional commits
    │
    ├── No releasable changes → nothing happens
    │
    └── Releasable changes found
         │
         ▼
    Creates/updates a release PR
    (version bump + changelog)
         │
         ▼
    You review and merge the PR
         │
         ▼
    release.yml triggers automatically:
         │
         ├── 1. Build (sdist + wheel)
         ├── 2. Collect contributors
         ├── 3. Generate release notes
         ├── 4. Publish to Test PyPI
         ├── 5. Pre-release checks (install + tests)
         ├── 6. Publish to PyPI
         └── 7. Create GitHub Release + tag
```

### Triggering a Release

1. Make commits to `main` using conventional commit format:
   ```
   feat(sdk): add streaming support
   fix(cli): handle missing config gracefully
   ```

2. Wait for release-please to create a release PR (automatic on push to main)

3. Review the PR:
   - Check the generated CHANGELOG
   - Verify the version bump is correct
   - For CLI releases: verify the SDK pin matches

4. Merge the release PR — everything else is automated

### Manual Release (Emergency/Hotfix)

1. Go to **Actions** > **Package Release**
2. Click **Run workflow**
3. Select the package (`bog-agents` or `bog-agents-cli`)
4. Optionally check `dangerous-nonmain-release` for hotfix branches

### Release Order

When releasing both SDK and CLI:
1. Release the SDK first (bump version, merge release PR)
2. Update the CLI's SDK pin in `libs/cli/pyproject.toml`: `bog-agents==<new-version>`
3. Release the CLI

---

## Verify Your Package

After publishing, verify everything works:

```bash
# Create a clean virtual environment
python -m venv /tmp/test-bog-agents
source /tmp/test-bog-agents/bin/activate

# Test SDK
pip install bog-agents
python -c "
from bog_agents.graph import create_agent
agent = create_agent()
print(f'SDK loaded: {type(agent).__name__}')
print('bog-agents is working!')
"

# Test SDK with serve extra
pip install 'bog-agents[serve]'
python -c "
from bog_agents.serve import AgentServer, ServerConfig
print('Serve module loaded successfully')
"

# Test CLI
pip install bog-agents-cli
bog-agents --version
bog-agents --doctor

# Test CLI with a specific provider
pip install 'bog-agents-cli[anthropic]'

# Test all providers
pip install 'bog-agents-cli[all-providers]'

# Cleanup
deactivate
rm -rf /tmp/test-bog-agents
```

### Check PyPI Pages

- SDK: https://pypi.org/project/bog-agents/
- CLI: https://pypi.org/project/bog-agents-cli/

Verify:
- Description renders correctly (from README.md)
- Version number is correct
- Dependencies are listed
- Classifiers appear
- License is shown

---

## Test PyPI (Staging)

Always test on Test PyPI before the first real release:

```bash
# Upload to Test PyPI
twine upload --repository testpypi dist/*

# Install from Test PyPI (with real PyPI for dependencies)
pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  bog-agents

# Verify
python -c "import bog_agents; print(bog_agents.__version__)"
```

Test PyPI URL: https://test.pypi.org/project/bog-agents/

---

## Troubleshooting

### "File already exists" Error

PyPI does not allow re-uploading the same version. You must bump the version number.

```bash
# If you need to re-release, bump the patch version
# Edit pyproject.toml and _version.py, then rebuild
```

### "Invalid or non-existent authentication" Error

- Verify your token starts with `pypi-`
- Check the token is scoped to the correct project
- If using trusted publishing, verify the workflow name matches exactly

### Package Name Already Taken

PyPI package names are globally unique. Check availability at:
https://pypi.org/project/your-package-name/

If taken, you'll need a different name. PyPI normalizes names (underscores = hyphens = dots), so `bog_agents` and `bog-agents` are the same package.

### README Not Rendering on PyPI

PyPI only supports a subset of Markdown/RST. Common issues:
- Relative image links don't work (use absolute URLs to raw.githubusercontent.com)
- Some HTML tags are stripped
- Check with: `twine check dist/*`

### "Project not found" for Trusted Publishing

If PyPI says the project doesn't exist when configuring trusted publishing:
1. You need to upload the first version manually (or use "pending publisher")
2. Go to https://pypi.org/manage/account/publishing/ and use "Add a new pending publisher"
3. This registers the project name and configures trusted publishing in one step

### CI Release Failed After Merge

See `.github/RELEASING.md` for detailed recovery procedures, including:
- SDK pin mismatch
- Stuck `autorelease: pending` labels
- Re-releasing a version
- Yanking a bad release

---

## Quick Reference

| Task | Command |
|------|---------|
| Build SDK | `cd libs/bog-agents && uv run --group test python -m build` |
| Build CLI | `cd libs/cli && uv run --group test python -m build` |
| Check package | `twine check dist/*` |
| Upload to Test PyPI | `twine upload --repository testpypi dist/*` |
| Upload to PyPI | `twine upload dist/*` |
| Install SDK | `pip install bog-agents` |
| Install CLI | `pip install bog-agents-cli` |
| Install CLI (all providers) | `pip install 'bog-agents-cli[all-providers]'` |
| Install SDK (with serve) | `pip install 'bog-agents[serve]'` |
| Check version | `python -c "import bog_agents; print(bog_agents.__version__)"` |
| CLI version | `bog-agents --version` |
| CLI diagnostics | `bog-agents --doctor` |
