"""Build the standalone Windows app and a native Inno Setup installer."""

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
from tools.distribution import prepare_handoff

ROOT = Path(__file__).resolve().parent.parent


def find_compiler():
    explicit = os.environ.get("INNO_SETUP_COMPILER")
    candidates = [Path(explicit)] if explicit else []
    candidates += [ROOT / ".audit/tooling/inno/ISCC.exe"]
    on_path = shutil.which("ISCC.exe")
    if on_path:
        candidates.append(Path(on_path))
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates += [Path(base) / f"Inno Setup {version}/ISCC.exe" for version in (6, 7)]
            candidates += [Path(base) / f"Programs/Inno Setup {version}/ISCC.exe" for version in (6, 7)]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(
        "Не найден Inno Setup 6.7.3+. Выполните powershell -File tools/bootstrap_inno.ps1 или задайте INNO_SETUP_COMPILER."
    )


def validate_package(package):
    if not (package / "InviterStudio.exe").is_file() or not (package / "_internal").is_dir():
        raise RuntimeError("Нет полной сборки приложения: нужны InviterStudio.exe и _internal")
    for path in package.rglob("*"):
        relative = path.relative_to(package)
        if (
            any(part.lower() in {"data", "sessions", ".audit", ".git", "backups"} for part in relative.parts)
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
    parser.add_argument("--installer-only", action="store_true", help="Use the already built standalone application")
    options = parser.parse_args()
    compiler = find_compiler()
    assets = ROOT / "build/installer-assets"
    create_assets(assets)
    resource = assets / "version.txt"
    version_resource(resource)
    if not options.installer_only:
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
                "--noupx",
                "--name",
                "InviterStudio",
                "--icon",
                str(assets / "app.ico"),
                "--version-file",
                str(resource),
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
    validate_package(package)
    subprocess.run(
        [
            str(compiler),
            "/Qp",
            f"/DSourceRoot={ROOT}",
            f"/DAppVersion={VERSION}",
            f"/DSourcePackage={package}",
            str(ROOT / "tools/installer/inviter.iss"),
        ],
        cwd=ROOT,
        check=True,
        timeout=180,
    )
    relative = package.relative_to(ROOT).as_posix()
    (ROOT / "dist/release.json").write_text(
        json.dumps({"version": VERSION, "package": relative}, indent=2), encoding="utf-8"
    )
    batch_path = package.relative_to(ROOT / "dist").as_posix().replace("/", "\\")
    (ROOT / "dist/StartInviterStudio.bat").write_text(
        '@echo off\nsetlocal\nset "INVITER_HOME=%~dp0.."\nstart "" "%~dp0' + batch_path + '\\InviterStudio.exe"\n',
        encoding="ascii",
    )
    handoff = prepare_handoff(ROOT, ROOT / "dist/InviterStudioSetup.exe")
    print(f"Built: {package / 'InviterStudio.exe'}; handoff: {handoff}")


if __name__ == "__main__":
    main()
