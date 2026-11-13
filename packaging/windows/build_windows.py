"""Build the Windows installer ``dist/JRef-<version>-Setup.exe`` (runs on Linux or Windows).

Layout installed under ``%LOCALAPPDATA%\\Programs\\JRef``::

    JRef.exe     tiny NSIS launcher -> python\\pythonw.exe -m bibmanager
    JRef.ico     from the logo artwork (bibmanager/gui/assets, see packaging/branding)
    LICENSES.txt        third-party components and their licences
    python\\            private CPython 3.12 for Windows (conda-forge build) + site-packages
    app\\bibmanager\\   the application package;  app\\config.json  bundled defaults

Steps: (1) solve/download CPython win-64 from conda-forge with py-rattler; (2) install the pinned
win_amd64 wheels of ``requirements-windows.txt`` with ``pip --target --platform``; (3) copy the
application; (4) trim files the app never loads; (5) render the icon; (6) compile launcher and
installer with NSIS (``makensis``). Optional: ``--smoke-test`` installs and runs it under Wine.

Requirements on the build machine: Python >= 3.11 with PySide6 (icon rendering), py-rattler, pip,
Pillow, NSIS (``makensis``); Wine only for ``--smoke-test``.

    python packaging/windows/build_windows.py [--work DIR] [--smoke-test]
"""

from __future__ import annotations

import argparse
import asyncio
import email.parser
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PYTHON_SPEC = "python 3.12.*"

# Files and folders of the runtime the application never touches (saves ~60% of the size).
TRIM_RUNTIME = [
    "include", "libs", "Tools", "share", "conda-meta", "Scripts", "etc",
    "Lib/test", "Lib/idlelib", "Lib/tkinter", "Lib/turtledemo", "Lib/ensurepip", "Lib/lib2to3",
    "Library/lib/tcl8.6", "Library/lib/tk8.6", "Library/include", "Library/share",
    "DLLs/_tkinter.pyd", "Library/bin/tcl86t.dll", "Library/bin/tk86t.dll",
]  # fmt: skip
TRIM_QT_GLOBS = [
    "Qt6Quick*", "Qt6Qml*", "Qt6Designer*", "Qt6Pdf*", "Qt6Sql*", "Qt6Test*", "Qt6Help*", "Qt6Concurrent*",
    "Qt6Labs*", "Qt6ShaderTools*", "Qt6OpenGLWidgets*", "Qt6UiTools*", "Qt6Xml*",
    "QtQuick*", "QtQml*", "QtDesigner*", "QtPdf*", "QtSql*", "QtTest*", "QtHelp*", "QtConcurrent*",
    "QtOpenGLWidgets*", "QtUiTools*", "QtXml*",
    "designer.exe", "linguist.exe", "assistant.exe", "lupdate.exe", "lrelease.exe", "qml*.exe", "balsam*.exe",
    "rcc.exe", "uic.exe", "*.pyi", "qml", "include", "typesystems", "glue", "examples", "scripts",
    "plugins/qmltooling", "plugins/sqldrivers", "plugins/designer", "metatypes", "resources",
    # Not reachable from QtCore/QtGui/QtWidgets + platform/style/image plugins (checked with pefile):
    "opengl32sw.dll", "Qt6OpenGL*", "QtOpenGL*", "Qt6Network*", "QtNetwork*", "Qt6DBus*", "QtDBus*",
    "Qt6PrintSupport*", "QtPrintSupport*", "Qt6SvgWidgets*", "QtSvgWidgets*", "QtSvg.pyd", "pyside6qml*",
    "QtExampleIcons*", "plugins/imageformats/qpdf.dll", "plugins/tls", "plugins/networkinformation",
    "plugins/qmllint", "plugins/qmlls",
]  # fmt: skip


def run(cmd: list[str], **kw: object) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)  # type: ignore[call-overload]


def app_version() -> str:
    namespace: dict[str, str] = {}
    exec((REPO / "bibmanager" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return namespace["__version__"]


# --------------------------------------------------------------------------- 1. runtime
def fetch_runtime(prefix: Path) -> None:
    import rattler
    from rattler import GenericVirtualPackage, PackageName, Platform, Version
    from rattler.package.build_string import BuildString

    async def solve_and_install() -> None:
        records = await rattler.solve(
            sources=["conda-forge"],
            specs=[PYTHON_SPEC],
            platforms=[Platform("win-64"), Platform("noarch")],
            virtual_packages=[GenericVirtualPackage(PackageName("__win"), Version("0"), BuildString("0"))],
        )
        for record in records:
            print(f"  conda-forge {record.name.normalized} {record.version} {record.build}")
        await rattler.install(records, target_prefix=prefix, platform=Platform("win-64"))

    asyncio.run(solve_and_install())


# --------------------------------------------------------------------------- 2. wheels
def install_wheels(site_packages: Path, wheels_dir: Path) -> None:
    requirements = HERE / "requirements-windows.txt"
    common = ["--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp", "--only-binary=:all:"]
    run([sys.executable, "-m", "pip", "download", "-q", "-r", str(requirements), "-d", str(wheels_dir), *common])
    wheels = sorted(str(p) for p in wheels_dir.glob("*.whl"))
    run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--no-compile", "--target", str(site_packages),
         *common, *wheels])  # fmt: skip
    shutil.rmtree(site_packages / "bin", ignore_errors=True)


# --------------------------------------------------------------------------- 3-4. app + trimming
def copy_app(stage: Path) -> None:
    app = stage / "app"
    shutil.copytree(
        REPO / "bibmanager", app / "bibmanager", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "mobile")
    )  # bibmanager/mobile is the Android interface (Kivy): not part of the desktop installation
    shutil.copy2(REPO / "config.json", app / "config.json")
    site = stage / "python" / "Lib" / "site-packages"
    (site / "bibmanager-app.pth").write_text("../../../app\n", encoding="utf-8")


def trim(stage: Path) -> None:
    runtime = stage / "python"
    for rel in TRIM_RUNTIME:
        target = runtime / rel
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for pdb in runtime.rglob("*.pdb"):
        pdb.unlink()
    pyside = runtime / "Lib" / "site-packages" / "PySide6"
    for pattern in TRIM_QT_GLOBS:
        for target in pyside.glob(pattern):
            shutil.rmtree(target) if target.is_dir() else target.unlink()
    translations = pyside / "translations"
    if translations.is_dir():
        for qm in translations.glob("*.qm"):
            if not qm.name.startswith(("qtbase_en", "qtbase_it")):
                qm.unlink()
    for cache in list(runtime.rglob("__pycache__")):
        shutil.rmtree(cache)


def write_licenses(stage: Path) -> None:
    site = stage / "python" / "Lib" / "site-packages"
    lines = [
        "JRef bundles the following third-party components (licence as declared by each package).",
        "Licence texts are in python\\LICENSE_PYTHON.txt and in each package's *.dist-info folder.",
        "",
        "Python (conda-forge build)        PSF-2.0",
    ]
    parser = email.parser.Parser()
    for meta in sorted(site.glob("*.dist-info/METADATA")):
        message = parser.parsestr(meta.read_text(encoding="utf-8", errors="replace"), headersonly=True)
        lic = message.get("License-Expression") or message.get("License") or ""
        if not lic or len(lic) > 60:
            classifiers = [c.split("::")[-1].strip() for c in message.get_all("Classifier") or [] if "License" in c]
            lic = ", ".join(classifiers) or lic[:60]
        lines.append(f"{message['Name']} {message['Version']}".ljust(34) + lic)
    lines += [
        "",
        "Note: PyMuPDF is licensed under AGPL-3.0 (or a commercial licence from Artifex). Redistributing this",
        "installer to others makes the AGPL apply to JRef as a whole (source must be made available).",
    ]
    (stage / "LICENSES.txt").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


# --------------------------------------------------------------------------- 5. icon + installer artwork
ASSETS = REPO / "bibmanager" / "gui" / "assets"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_icon(target: Path) -> None:
    """Multi-resolution .ico of the simplified mark, drawn by ``bibmanager.gui.icons.paint_app_icon``."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(REPO))
    from PIL import Image
    from PySide6.QtWidgets import QApplication

    from bibmanager.gui import icons

    app = QApplication.instance() or QApplication([])
    images = []
    with tempfile.TemporaryDirectory() as tmp:
        for size in ICON_SIZES:
            png = Path(tmp) / f"{size}.png"
            icons.app_icon_image(size).save(str(png))
            images.append(Image.open(png).convert("RGBA"))
            images[-1].load()
    images[-1].save(target, format="ICO", sizes=[im.size for im in images], append_images=images[:-1])
    del app


def render_wizard_bitmap(target: Path) -> None:
    """164 x 314 BMP for the installer's welcome/finish pages: the logo on the tile's dark background."""
    from PIL import Image

    canvas = Image.new("RGB", (164, 314), (29, 36, 52))
    tile = Image.open(ASSETS / "jref_tile.png").convert("RGBA").resize((140, 140), Image.Resampling.LANCZOS)
    canvas.paste(tile, (12, 70), tile)
    canvas.save(target, format="BMP")


# --------------------------------------------------------------------------- 6. NSIS
def folder_size_kb(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) // 1024


def build(work: Path, out_dir: Path) -> Path:
    version = app_version()
    stage = work / "stage"
    runtime_cache = work / "runtime"
    if not (runtime_cache / "python.exe").exists():
        fetch_runtime(runtime_cache)
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(runtime_cache, stage / "python")
    wheels = work / "wheels"
    shutil.rmtree(wheels, ignore_errors=True)
    install_wheels(stage / "python" / "Lib" / "site-packages", wheels)
    copy_app(stage)
    trim(stage)
    write_licenses(stage)
    render_icon(stage / "JRef.ico")
    render_wizard_bitmap(work / "wizard.bmp")
    shutil.copy2(HERE / "launcher.nsi", work / "launcher.nsi")
    shutil.copy2(stage / "JRef.ico", work / "JRef.ico")
    run(
        [
            "makensis",
            "-V2",
            f"-DVERSION={version}",
            f"-DOUTFILE={stage / 'JRef.exe'}",
            str(work / "launcher.nsi"),
        ]
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    setup = out_dir / f"JRef-{version}-Setup.exe"
    run([
        "makensis", "-V2", f"-DVERSION={version}", f"-DSTAGE={stage}", f"-DOUTFILE={setup}",
        f"-DWIZARD_BMP={work / 'wizard.bmp'}", f"-DSIZE_KB={folder_size_kb(stage)}", str(HERE / "installer.nsi"),
    ])  # fmt: skip
    print(
        f"\nInstaller: {setup} ({setup.stat().st_size / 1e6:.1f} MB); staged size {folder_size_kb(stage) / 1024:.0f} MB"
    )
    return setup


# --------------------------------------------------------------------------- smoke test (Wine)
def smoke_test(setup: Path, work: Path) -> None:
    env = dict(os.environ, WINEPREFIX=str(work / "wineprefix"), WINEDEBUG="-all")
    target = "C:\\JRefSmoke"
    run(["wine", str(setup), "/S", f"/D={target}"], env=env)
    python = work / "wineprefix" / "drive_c" / "JRefSmoke" / "python" / "python.exe"
    check = (
        "import sys, sqlite3, ssl, bibmanager, bibmanager.gui.app, PySide6.QtWidgets, pymupdf, httpx;"
        "print(sys.version.split()[0], bibmanager.__version__, PySide6.__version__, pymupdf.VersionBind,"
        " sqlite3.sqlite_version)"
    )
    run(["wine", str(python), "-c", check], env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", type=Path, default=REPO / "build" / "windows", help="scratch directory")
    parser.add_argument("--out", type=Path, default=REPO / "dist", help="where the Setup.exe is written")
    parser.add_argument("--smoke-test", action="store_true", help="install silently and import everything under Wine")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    setup = build(args.work.resolve(), args.out.resolve())
    if args.smoke_test:
        smoke_test(setup, args.work.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
