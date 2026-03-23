# Publishing to PyPI

Step-by-step guide to publish `bog-agents` and `bog-agents-cli` so users can `pip install` them.

## 1. Set Up Trusted Publishing on PyPI

Trusted publishing lets GitHub Actions publish directly to PyPI — no API tokens needed.

### For `bog-agents` (SDK):

1. Go to https://pypi.org/manage/account/publishing/
2. Under **"Add a new pending publisher"**, fill in:

   | Field | Value |
   |-------|-------|
   | PyPI project name | `bog-agents` |
   | Owner | `bogware` |
   | Repository name | `bog-agents` |
   | Workflow name | `release.yml` |
   | Environment name | _(leave blank)_ |

3. Click **Add**

### For `bog-agents-cli` (CLI):

Repeat the same steps with PyPI project name = `bog-agents-cli`.

> **That's it for PyPI setup.** No API tokens, no secrets to configure in GitHub. The `release.yml` workflow uses OIDC to authenticate automatically.

## 2. First Release

Since the package names aren't registered on PyPI yet, the first release uses the "pending publisher" flow above. Once you've added the pending publishers:

### Option A: Trigger via release-please (recommended)

1. Merge a `feat` or `fix` commit to `main`:
   ```bash
   git commit --allow-empty -m "feat(sdk): initial release"
   git push origin main
   ```
2. release-please creates a release PR — merge it
3. The pipeline builds, tests, and publishes automatically

### Option B: Manual first upload

If you prefer to test locally first:

```bash
cd libs/bog-agents
uv build

# Upload to PyPI (requires a one-time API token from pypi.org/manage/account/token/)
pip install twine
TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-your-token twine upload dist/*
```

Repeat for `libs/cli`.

## 3. Verify

```bash
pip install bog-agents
python -c "import bog_agents; print('SDK works!')"

pip install bog-agents-cli
bog-agents --version
```

PyPI pages:
- https://pypi.org/project/bog-agents/
- https://pypi.org/project/bog-agents-cli/

## 4. Ongoing Releases

After the first publish, everything is automated:

```
feat/fix commit → main → release-please PR → merge → PyPI publish
```

See [`.github/RELEASING.md`](.github/RELEASING.md) for full details.

## VS Code Extension

The VS Code extension publishes to the VS Code Marketplace separately:

1. Get a Personal Access Token from https://dev.azure.com/ (Organization > User Settings > Personal Access Tokens)
   - Scope: `Marketplace > Manage`
2. Add it as `VSCE_PAT` in GitHub repo secrets (Settings > Secrets > Actions)
3. Go to **Actions** > **VS Code Extension** > **Run workflow** with `publish: true`

## Quick Reference

| Task | Command |
|------|---------|
| Install SDK | `pip install bog-agents` |
| Install CLI | `pip install bog-agents-cli` |
| Install CLI (all providers) | `pip install 'bog-agents-cli[all-providers]'` |
| Build locally | `cd libs/bog-agents && uv build` |
| Check version | `python -c "import bog_agents; print(bog_agents.__version__)"` |
| CLI version | `bog-agents --version` |
| CLI diagnostics | `bog-agents --doctor` |
