# Packaging (ROADMAP #61)

Everything here turns the PyPI packages into something a person can install
without knowing what `pipx` is.

| Piece | What it does | Who runs it |
|---|---|---|
| `../install.ps1` | Windows one-liner: `irm https://raw.githubusercontent.com/bogware/bog-agents/main/install.ps1 \| iex`. Picks `uv tool` → `pipx` → `pip --user`, installs uv (and a Python) when the machine has none, warns about Microsoft Store execution aliases (`python`, `pwsh`), fixes PATH, ends with `bog-agents --doctor`. | users |
| `../install.sh` | The same for macOS / Linux: `curl -LsSf https://raw.githubusercontent.com/bogware/bog-agents/main/install.sh \| sh`. | users |
| `pyinstaller/` | `bog-agents.spec` + `build.py`: a standalone onedir bundle (no Python required) built by the `windows-standalone` job in `release.yml` for `bog-agents-cli` releases and attached to the GitHub release as `bog-agents-<v>-windows-x64.zip` (+ `.sha256`). The spec walks the installed dependency closure and collects every package plus its metadata, so lazy provider imports and `--doctor`'s package checks work when frozen. | release job |
| `winget/generate_manifest.py` | Writes the winget manifest trio for a published zip (portable nested installer, alias `bog-agents`). Validate with `winget validate`, submit with `wingetcreate submit` or a PR to `microsoft/winget-pkgs`. | maintainer, after a release |
| `homebrew/bog-agents-cli.rb` | Formula for the `bogware/homebrew-tap` tap (`brew install bogware/tap/bog-agents-cli`). Regenerate resources with `brew update-python-resources`. | maintainer, after a release |

## Release checklist for a CLI version

1. release-please cuts `bog-agents-cli==X.Y.Z`; `release.yml` publishes to PyPI and, for the
   CLI, runs `windows-standalone` on `windows-latest` which attaches the zip and its sha256.
2. `python packaging/winget/generate_manifest.py --version X.Y.Z --sha256 <from the sidecar>`
   → `winget validate --manifest packaging/winget/manifests/b/bogware/bog-agents-cli/X.Y.Z`
   → submit.
3. Update `homebrew/bog-agents-cli.rb` (`url`, `sha256`, resources) and push it to the tap.

## Not done yet (needs the org's accounts)

- **Code signing.** The zip is unsigned; SmartScreen warns on first run. Azure Trusted Signing
  needs a certificate profile and the `AZURE_*` secrets — the release job has the step
  commented out with the exact action to enable.
- **winget submission** and the **Homebrew tap** need the maintainer's GitHub account; the
  files here are ready to submit.
