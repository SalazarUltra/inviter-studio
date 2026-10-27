"""Build the standalone Windows app and the portable distribution package."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

from core.version import APP_COPYRIGHT, VERSION
from tools.build_assets import create_assets
from tools.distribution import prepare_portable_handoff

ROOT = Path(__file__).resolve().parent.parent

# --- Portable size ------------------------------------------------------------------------------------------
# The ZIP is mostly the PyInstaller onedir runtime, so the size is controlled here and in tools/distribution.py:
#  * EXCLUDED_MODULES keep optional Pillow codecs and dev-only stdlib packages out of the analysis. The app only
#    draws icons with ImageDraw, never decodes AVIF/WebP or uses colour management; Pillow guards each of these
#    imports (`try: from . import _avif ... except ImportError`), so excluding them is safe. PIL._avif alone is ~7.5 MB.
#  * prune_package() is a second, defensive pass over the finished folder (stray caches, unused Tk locales,
#    optional codecs if an exclude was ignored). Python 3.14 embeds the Tcl/Tk 9 scripts in the DLLs, so there is
#    normally no locale folder to strip; the Tcl 8.6 layout (older Python) is handled when present.
#  * UPX is deliberately NOT used (`--noupx`): packed executables trigger antivirus false positives and Smart App
#    Control blocks, which matters more than the few MB it would save. Add it only if a trusted signing
#    certificate is configured (INVITER_SIGNING_THUMBPRINT) and the package is re-tested.
#  * tools/distribution.py writes the ZIP with Deflate level 9 (best ratio; build time is not a concern).
EXCLUDED_MODULES = (
    "PIL._avif",
    "PIL._webp",
    "PIL._imagingcms",
    "tkinter.test",
    "test",
    "idlelib",
    "lib2to3",
    "pydoc_data",
)
# Tcl 8.6 layout only: keep English and Russian message catalogs, drop the other ~100 locales.
KEPT_TK_LOCALES = ("en", "ru")
OPTIONAL_PIL_BINARIES = ("_avif", "_webp", "_imagingcms")


def find_signtool():
    explicit = os.environ.get("SIGNTOOL_PATH")
    candidates = [Path(explicit)] if explicit else []
    on_path = shutil.which("signtool.exe")
    if on_path:
        candidates.append(Path(on_path))
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(variable)
        if base:
            kits = Path(base) / "Windows Kits" / "10" / "bin"
            if kits.is_dir():
                # Versioned SDK folders: pick the newest installed kit.
                candidates += sorted(kits.glob("*/x64/signtool.exe"), reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(
        "signtool.exe не найден. Установите Windows SDK или задайте SIGNTOOL_PATH на signtool.exe для подписи сборки."
    )


def sign_artifacts(paths):
    """Sign release artifacts when a certificate is configured; otherwise build unsigned.

    Set INVITER_SIGNING_THUMBPRINT to the SHA-1 thumbprint of a certificate from the
    current user's store (optional INVITER_TIMESTAMP_URL overrides the RFC3161 server).
    """
    thumbprint = os.environ.get("INVITER_SIGNING_THUMBPRINT", "").strip()
    if not thumbprint:
        print("Подпись отключена: INVITER_SIGNING_THUMBPRINT не задан (сборка остаётся без подписи).")
        return
    signtool = find_signtool()
    timestamp = os.environ.get("INVITER_TIMESTAMP_URL", "http://timestamp.digicert.com")
    for artifact in paths:
        if not artifact.is_file():
            continue
        subprocess.run(
            [
                str(signtool),
                "sign",
                "/fd",
                "SHA256",
                "/td",
                "SHA256",
                "/tr",
                timestamp,
                "/sha1",
                thumbprint,
                str(artifact),
            ],
            check=True,
            timeout=180,
        )
        print(f"Подписан: {artifact}")


def prune_package(package: Path) -> int:
    """Remove files the app never loads from a finished onedir package; returns the bytes freed.

    Safe to run repeatedly. Only touches `_internal`, never user-data folders.
    """
    internal = package / "_internal"
    freed = 0

    def drop(path: Path) -> None:
        nonlocal freed
        if path.is_dir():
            freed += sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            freed += path.stat().st_size
            path.unlink(missing_ok=True)

    for cache in list(internal.rglob("__pycache__")):
        drop(cache)
    for stem in OPTIONAL_PIL_BINARIES:
        for binary in list((internal / "PIL").glob(f"{stem}*.pyd")):
            drop(binary)
    for tk_data in ("_tcl_data", "_tk_data", "tcl", "tk"):
        messages = internal / tk_data / "msgs"
        if not messages.is_dir():
            continue
        for catalog in list(messages.glob("*.msg")):
            language = catalog.stem.split("_")[0].lower()
            if language not in KEPT_TK_LOCALES:
                drop(catalog)
    return freed


def validate_package(package):
    if not (package / "InviterStudio.exe").is_file() or not (package / "_internal").is_dir():
        raise RuntimeError("Нет полной сборки приложения: нужны InviterStudio.exe и _internal")
    for path in package.rglob("*"):
        relative = path.relative_to(package)
        if (
            any(part.lower() in {"data", "sessions", "profile", "backups", ".audit", ".git"} for part in relative.parts)
            or path.is_file()
            and (
                path.name.lower().startswith(".env")
                or ".session" in path.name.lower()
                or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
                or path.name.lower().endswith((".db-wal", ".db-shm"))
            )
        ):
            raise RuntimeError(f"В сборке обнаружены пользовательские данные: {relative}")


def version_resource(path):
    digits = tuple(int(value) for value in VERSION.split(".")) + (0,)
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={digits}, prodvers={digits}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
  kids=[StringFileInfo([StringTable('040904B0', [StringStruct('CompanyName','Inviter Studio'), StringStruct('LegalCopyright','{APP_COPYRIGHT}'), StringStruct('FileDescription','Inviter Studio'), StringStruct('FileVersion','{VERSION}'), StringStruct('InternalName','InviterStudio'), StringStruct('OriginalFilename','InviterStudio.exe'), StringStruct('ProductName','Inviter Studio'), StringStruct('ProductVersion','{VERSION}')])]), VarFileInfo([VarStruct('Translation',[1033,1200])])]
)
""",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-only", action="store_true", help="Reuse the already built standalone application")
    options = parser.parse_args()
    assets = ROOT / "build/installer-assets"
    create_assets(assets)
    resource = assets / "version.txt"
    version_resource(resource)
    if not options.package_only:
        release_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        release_root = ROOT / "dist/releases" / release_id
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--onedir",
                "--windowed",
                "--noupx",  # see "Portable size" above: UPX is intentionally off
                "--name",
                "InviterStudio",
                "--icon",
                str(assets / "app.ico"),
                "--version-file",
                str(resource),
                *[arg for module in EXCLUDED_MODULES for arg in ("--exclude-module", module)],
                "--collect-data",
                "customtkinter",
                "--add-data",
                f"{ROOT / 'ui/assets'}:ui/assets",
                "--specpath",
                "build",
                "--distpath",
                str(release_root),
                "main.py",
            ],
            cwd=ROOT,
            check=True,
        )
        package = release_root / "InviterStudio"
    else:
        manifest = ROOT / "dist/release.json"
        package = (
            ROOT / json.loads(manifest.read_text(encoding="utf-8"))["package"]
            if manifest.is_file()
            else ROOT / "dist/InviterStudio"
        )
        if not package.resolve().is_relative_to((ROOT / "dist").resolve()):
            raise RuntimeError("Путь сборки должен находиться внутри dist")
    freed = prune_package(package)
    if freed:
        print(f"Убрано лишнего из сборки: {freed / 1024 / 1024:.1f} МБ")
    validate_package(package)
    # Signing the executable itself is what Windows Smart App Control trusts;
    # a wrapper archive inherits nothing.
    sign_artifacts([package / "InviterStudio.exe"])
    relative = package.relative_to(ROOT).as_posix()
    (ROOT / "dist/release.json").write_text(
        json.dumps({"version": VERSION, "package": relative}, indent=2), encoding="utf-8"
    )
    batch_path = package.relative_to(ROOT / "dist").as_posix().replace("/", "\\")
    (ROOT / "dist/StartInviterStudio.bat").write_text(
        '@echo off\nsetlocal\nset "INVITER_HOME=%~dp0.."\nstart "" "%~dp0' + batch_path + '\\InviterStudio.exe"\n',
        encoding="ascii",
    )
    portable = prepare_portable_handoff(ROOT, package)
    print(f"Built: {package / 'InviterStudio.exe'}; portable: {portable}")


if __name__ == "__main__":
    main()
