"""Gera o instalador do Pingo Dock: dist\\PingoDock-Setup-<versão>.exe

Uso (na pasta do projeto):
    .venv\\Scripts\\python.exe installer\\build.py [--update-source PASTA_OU_URL]

--update-source: onde os PCs vão procurar versões novas (ex.: \\\\servidor\\TI\\PingoDock).
Fica gravado no instalador; para publicar uma versão, copie o Setup e o
latest.json gerados em dist\\ para essa pasta.

Etapas: ícone → PyInstaller (PingoDock.exe com Python e Qt embutidos) → remoção
do que o dock não usa → Inno Setup (Setup.exe) → latest.json.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# A versão vem do dock.py (APP_VERSION): app e instalador nunca divergem.
VERSION = re.search(r'^APP_VERSION = "([^"]+)"', (ROOT / "dock.py").read_text(encoding="utf-8"),
                    re.M).group(1)
INSTALLER = ROOT / "installer"
BUILD = ROOT / "build"
APP = BUILD / "dist" / "PingoDock"
ICON = INSTALLER / "pingodock.ico"
FONTS = ROOT / "assets" / "fonts" / "poppins"

# Partes do Qt/Python que entram de carona mas o dock não usa (~60 MB).
UNUSED = [
    "PySide6/opengl32sw.dll",            # OpenGL por software (o dock não usa OpenGL)
    "PySide6/translations",              # janelas de arquivo são as nativas do Windows
    "PySide6/Qt6Pdf.dll", "PySide6/Qt6OpenGL.dll", "PySide6/Qt6VirtualKeyboard.dll",
    "PySide6/Qt6Quick.dll", "PySide6/Qt6Qml.dll", "PySide6/Qt6QmlMeta.dll",
    "PySide6/Qt6QmlModels.dll", "PySide6/Qt6QmlWorkerScript.dll",
    "PySide6/plugins/generic", "PySide6/plugins/networkinformation",
    "PySide6/plugins/platforminputcontexts", "PySide6/plugins/tls", "PySide6/plugins/styles",
    "PySide6/plugins/platforms/qdirect2d.dll", "PySide6/plugins/platforms/qminimal.dll",
    "PySide6/plugins/platforms/qoffscreen.dll",
    "PySide6/plugins/imageformats/qpdf.dll", "PySide6/plugins/imageformats/qgif.dll",
    "PySide6/plugins/imageformats/qicns.dll", "PySide6/plugins/imageformats/qtga.dll",
    "PySide6/plugins/imageformats/qtiff.dll", "PySide6/plugins/imageformats/qwbmp.dll",
    "PySide6/plugins/imageformats/qwebp.dll",
    "libcrypto-3.dll", "libcrypto-3-x64.dll", "libssl-3-x64.dll", "_hashlib.pyd",
    "_bz2.pyd", "_lzma.pyd",  # (_socket/select ficam: atualização por http)
]


def run(*cmd) -> None:
    print(">", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT)


def find_iscc() -> Path:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    sys.exit("Inno Setup 6 não encontrado. Instale com:  winget install JRSoftware.InnoSetup --scope user")


def build_taskbar_guard(destination: Path) -> None:
    zig = os.environ.get("PINGODOCK_ZIG") or shutil.which("zig")
    if not zig:
        sys.exit("Zig não encontrado. Inclua zig.exe no PATH ou defina PINGODOCK_ZIG.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(zig, "cc", "-target", "x86_64-windows-gnu", "-std=c11", "-Os", "-s",
        "-municode", "-Wl,--subsystem,windows", INSTALLER / "taskbar_guard.c", "-o", destination)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    py = sys.executable
    guard = BUILD / "native" / "PingoDockTaskbar.exe"
    build_taskbar_guard(guard)
    run(py, INSTALLER / "make_icon.py", ICON)

    run(py, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", "PingoDock", "--icon", ICON,
        "--add-data", f"{FONTS}:assets/fonts/poppins",
        "--distpath", BUILD / "dist", "--workpath", BUILD / "work", "--specpath", BUILD,
        "--exclude-module", "tkinter", "--exclude-module", "unittest",
        "--exclude-module", "pydoc", "--exclude-module", "xmlrpc",
        "--exclude-module", "ssl", "--exclude-module", "asyncio",
        ROOT / "dock.py")

    internal = APP / "_internal"
    shutil.copy2(guard, APP / guard.name)
    before = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file())
    for rel in UNUSED:
        target = internal / rel
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    # O Windows 10/11 já fornece ICU e UCRT compatíveis com as APIs do sistema.
    # Cópias coletadas de outros programas podem ocultar as DLLs corretas.
    for pattern in ("api-ms-win-*.dll", "ucrtbase.dll", "icuuc.dll", "icudt*.dll"):
        for dll in internal.glob(pattern):
            dll.unlink()
    after = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file())
    print(f"pacote: {before / 2**20:.0f} MB → {after / 2**20:.0f} MB")

    # Licenças: a do Pingo Dock e as dos componentes de terceiros (exigidas pela LGPL/PSF).
    shutil.copy2(ROOT / "LICENSE", APP / "LICENSE.txt")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", APP / "THIRD_PARTY_NOTICES.md")
    shutil.copytree(ROOT / "licenses", APP / "licenses", dirs_exist_ok=True)

    # Pasta/URL de atualizações gravada ao lado do PingoDock.exe (o dock lê dali).
    baked = APP / "update_source.txt"
    if "--update-source" in sys.argv:
        source = sys.argv[sys.argv.index("--update-source") + 1]
        baked.write_text(source, encoding="utf-8")
        print(f"atualizações em: {source}")

    if "--no-setup" in sys.argv:
        return
    run(find_iscc(), f"/DAppVersion={VERSION}", INSTALLER / "pingodock.iss")
    setup_name = f"PingoDock-Setup-{VERSION}.exe"
    # Manifesto que os docks instalados leem para saber se há versão nova.
    (ROOT / "dist" / "latest.json").write_text(
        json.dumps({"version": VERSION, "setup": setup_name}, indent=2), encoding="utf-8")
    print(f"\nInstalador pronto: {ROOT / 'dist' / setup_name}")
    print("Para publicar: copie o Setup e o latest.json de dist\\ para a pasta de atualizações.")


if __name__ == "__main__":
    main()
