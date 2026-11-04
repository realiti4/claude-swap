"""Build the desktop app and its release archives.

    uv run --extra gui python packaging/build.py [--installer]

(Keep ``--extra gui`` on ``uv run``: a bare ``uv run`` re-syncs without it.)

Windows: build/dist/claude-swap/ (+ cswap.cmd), a portable .zip, and with
         --installer an Inno Setup installer (needs iscc on PATH).
macOS:   build/dist/claude-swap.app and a .dmg.

Artifacts land in build/release/.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
DIST = ROOT / "build" / "dist"
WORK = ROOT / "build" / "work"
RELEASE = ROOT / "build" / "release"


def _size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def _arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)


def pyinstaller() -> None:
    shutil.rmtree(DIST, ignore_errors=True)
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", str(PKG / "claude-swap.spec"),
            "--noconfirm", "--clean", "--distpath", str(DIST), "--workpath", str(WORK),
        ],
        check=True,
    )


def windows(ver: str, installer: bool) -> list[Path]:
    app = DIST / "claude-swap"
    # CRLF: cmd.exe misparses batch files with bare LF line endings.
    (app / "cswap.cmd").write_bytes((PKG / "cswap.cmd").read_text().strip().encode() + b"\r\n")
    out = [RELEASE / f"claude-swap-{ver}-windows-{_arch()}-portable.zip"]
    with zipfile.ZipFile(out[0], "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in sorted(app.rglob("*")):
            if f.is_file():
                zf.write(f, Path("claude-swap") / f.relative_to(app))
    if installer:
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe",  # per-user install
            Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        ]
        iscc = shutil.which("iscc") or next((str(p) for p in candidates if p.exists()), "ISCC.exe")
        subprocess.run(
            [
                iscc, f"/DAppVersion={ver}", f"/DSourceDir={app}", f"/DOutputDir={RELEASE}",
                f"/DOutputName=claude-swap-{ver}-windows-{_arch()}-setup", str(PKG / "windows" / "installer.iss"),
            ],
            check=True,
        )
        out.append(RELEASE / f"claude-swap-{ver}-windows-{_arch()}-setup.exe")
    return out


def macos(ver: str) -> list[Path]:
    app = DIST / "claude-swap.app"
    # Ad-hoc signature: required on Apple silicon; users still confirm the
    # first launch (right-click → Open) since the app is not notarized.
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    staging = WORK / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    subprocess.run(["ditto", str(app), str(staging / "claude-swap.app")], check=True)
    (staging / "Applications").symlink_to("/Applications")
    dmg = RELEASE / f"claude-swap-{ver}-macos-{_arch()}.dmg"
    subprocess.run(
        [
            "hdiutil", "create", "-volname", "claude-swap", "-srcfolder", str(staging),
            "-format", "ULMO", "-ov", str(dmg),
        ],
        check=True,
    )
    return [dmg]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", action="store_true", help="Windows: also build the Inno Setup installer")
    args = parser.parse_args()

    ver = version("claude-swap")
    pyinstaller()
    RELEASE.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        bundle, artifacts = DIST / "claude-swap", windows(ver, args.installer)
    elif sys.platform == "darwin":
        bundle, artifacts = DIST / "claude-swap.app", macos(ver)
    else:
        raise SystemExit("The desktop build targets Windows and macOS only.")

    print(f"\nbundle   {_size_mb(bundle):6.1f} MB  {bundle}")
    for path in artifacts:
        print(f"artifact {_size_mb(path):6.1f} MB  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
