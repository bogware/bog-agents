"""Build the standalone `bog-agents` bundle and zip it (ROADMAP #61).

Run from anywhere with the CLI's environment active (the Windows release job
does `uv run --with pyinstaller python packaging/pyinstaller/build.py` inside
`libs/cli` after `uv sync --extra all-providers`):

    python packaging/pyinstaller/build.py --dist dist/standalone

Produces `<dist>/bog-agents/` (onedir), `<dist>/bog-agents-<version>-<os>-<arch>.zip`
and a `.sha256` sidecar, then smoke-tests the frozen exe (`--version`,
`command "/version"`). Exit code is non-zero when any step fails, so the job
never publishes a bundle that cannot start.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "bog-agents.spec"


def _version() -> str:
    try:
        return md.version("bog-agents-cli")
    except md.PackageNotFoundError:
        return "0.0.0"


def _platform_tag() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    name = {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(system, system)
    return f"{name}-{arch}"


def build(dist: Path, work: Path) -> Path:
    """Run PyInstaller with the shared spec; return the onedir folder."""
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        str(SPEC),
    ]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)  # noqa: S603 - fixed argv
    folder = dist / "bog-agents"
    if not folder.is_dir():
        msg = f"PyInstaller did not produce {folder}"
        raise SystemExit(msg)
    return folder


def smoke(folder: Path) -> None:
    """Start the frozen exe twice; a bundle that cannot print its version is not shippable."""
    exe = folder / ("bog-agents.exe" if platform.system() == "Windows" else "bog-agents")
    for args in (["--version"], ["command", "/version"]):
        result = subprocess.run([str(exe), *args], capture_output=True, text=True, check=False, timeout=180)  # noqa: S603 - our own exe
        print(f"+ {exe.name} {' '.join(args)} -> exit {result.returncode}: {result.stdout.strip().splitlines()[:2]}", flush=True)
        if result.returncode != 0 or "bog-agents-cli" not in result.stdout:
            print(result.stderr, file=sys.stderr)
            msg = f"frozen bundle failed smoke test: {args}"
            raise SystemExit(msg)


def zip_bundle(folder: Path, dist: Path, version: str) -> Path:
    """Zip the onedir folder (top-level `bog-agents/`) and write a sha256 sidecar."""
    archive = dist / f"bog-agents-{version}-{_platform_tag()}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(folder.rglob("*")):
            zf.write(path, path.relative_to(folder.parent).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (archive.with_suffix(".zip.sha256")).write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"+ wrote {archive} ({archive.stat().st_size / 1e6:.1f} MB) sha256={digest}", flush=True)
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=Path("dist/standalone"), help="output directory")
    parser.add_argument("--work", type=Path, default=Path("build/pyinstaller"), help="PyInstaller work directory")
    parser.add_argument("--no-zip", action="store_true", help="skip the zip + sha256 step")
    args = parser.parse_args(argv)
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except ImportError:
            print("PyInstaller is not installed; run with `uv run --with pyinstaller ...`", file=sys.stderr)
            return 2
    args.dist.mkdir(parents=True, exist_ok=True)
    folder = build(args.dist.resolve(), args.work.resolve())
    smoke(folder)
    if not args.no_zip:
        zip_bundle(folder, args.dist.resolve(), _version())
    return 0


if __name__ == "__main__":
    sys.exit(main())
