"""Generate the winget manifest trio for a published standalone Windows zip (ROADMAP #61).

winget cannot install from PyPI, so the manifest points at the `bog-agents-<v>-windows-x64.zip`
the release workflow attaches to the GitHub release and declares the exe inside it as a
*portable* nested installer (an alias `bog-agents` is put on PATH by winget itself).

    python packaging/winget/generate_manifest.py --version 0.9.14 \\
        --sha256 $(cut -d' ' -f1 bog-agents-0.9.14-windows-x64.zip.sha256)

writes `packaging/winget/manifests/b/bogware/bog-agents-cli/0.9.14/*.yaml`. Validate with
`winget validate --manifest <dir>`, test with `winget install --manifest <dir>`, then open a
PR against microsoft/winget-pkgs (`wingetcreate submit` does it in one step). The zip is
unsigned until Azure Trusted Signing is wired into the release job (needs the org's
certificate profile); SmartScreen will warn on first run until then.
"""

from __future__ import annotations

import argparse
from pathlib import Path

PACKAGE_ID = "bogware.bog-agents-cli"
PUBLISHER = "bogware"
MONIKER = "bog-agents"
REPO = "https://github.com/bogware/bog-agents"
MANIFEST_VERSION = "1.6.0"


def release_url(version: str) -> str:
    """Download URL of the standalone zip attached to the GitHub release for `version`."""
    return f"{REPO}/releases/download/bog-agents-cli%3D%3D{version}/bog-agents-{version}-windows-x64.zip"


def manifests(version: str, sha256: str, *, url: str | None = None) -> dict[str, str]:
    """Return `{filename: yaml}` for the version, installer and default-locale manifests."""
    url = url or release_url(version)
    version_yaml = f"""# yaml-language-server: $schema=https://aka.ms/winget-manifest.version.{MANIFEST_VERSION}.schema.json
PackageIdentifier: {PACKAGE_ID}
PackageVersion: {version}
DefaultLocale: en-US
ManifestType: version
ManifestVersion: {MANIFEST_VERSION}
"""
    installer_yaml = f"""# yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.{MANIFEST_VERSION}.schema.json
PackageIdentifier: {PACKAGE_ID}
PackageVersion: {version}
InstallerType: zip
NestedInstallerType: portable
NestedInstallerFiles:
  - RelativeFilePath: bog-agents\\bog-agents.exe
    PortableCommandAlias: bog-agents
Commands:
  - bog-agents
Installers:
  - Architecture: x64
    InstallerUrl: {url}
    InstallerSha256: {sha256.upper()}
ManifestType: installer
ManifestVersion: {MANIFEST_VERSION}
"""
    locale_yaml = f"""# yaml-language-server: $schema=https://aka.ms/winget-manifest.defaultLocale.{MANIFEST_VERSION}.schema.json
PackageIdentifier: {PACKAGE_ID}
PackageVersion: {version}
PackageLocale: en-US
Publisher: {PUBLISHER}
PublisherUrl: {REPO}
PublisherSupportUrl: {REPO}/issues
PackageName: Bog Agents CLI
PackageUrl: {REPO}
License: MIT
LicenseUrl: {REPO}/blob/main/LICENSE
ShortDescription: A coding agent that lives in your terminal, built on LangGraph.
Description: |-
  Bog Agents CLI is a terminal coding agent with file tools, a real shell, git,
  sub-agents, plan mode and governed autonomy (agent teams, cost caps,
  proof-of-work evidence). Works with any LLM provider; nothing leaves the
  machine when pointed at a local model.
Moniker: {MONIKER}
Tags:
  - ai
  - agent
  - cli
  - coding
  - langgraph
ReleaseNotesUrl: {REPO}/releases/tag/bog-agents-cli%3D%3D{version}
ManifestType: defaultLocale
ManifestVersion: {MANIFEST_VERSION}
"""
    return {
        f"{PACKAGE_ID}.yaml": version_yaml,
        f"{PACKAGE_ID}.installer.yaml": installer_yaml,
        f"{PACKAGE_ID}.locale.en-US.yaml": locale_yaml,
    }


def write_manifests(version: str, sha256: str, *, root: Path, url: str | None = None) -> Path:
    """Write the manifest trio under `root/manifests/b/bogware/bog-agents-cli/<version>/`."""
    out_dir = root / "manifests" / "b" / PUBLISHER / "bog-agents-cli" / version
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in manifests(version, sha256, url=url).items():
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="bog-agents-cli version, e.g. 0.9.14")
    parser.add_argument("--sha256", required=True, help="SHA-256 of the windows-x64 zip (see the .sha256 sidecar)")
    parser.add_argument("--url", default=None, help="override the installer URL (default: the GitHub release asset)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent, help="output root (default: packaging/winget)")
    args = parser.parse_args(argv)
    if len(args.sha256) != 64:
        parser.error("--sha256 must be 64 hex characters")
    out_dir = write_manifests(args.version, args.sha256, root=args.root, url=args.url)
    print(f"wrote {out_dir}")
    print(f"validate: winget validate --manifest {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
