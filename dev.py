"""Execute dev.py no ambiente virtual para recarregar o dock ao salvar."""

import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import time
from ctypes import wintypes

from taskbar import WindowsTaskbar


def main():
    dock = Path(__file__).with_name("dock.py")
    env = os.environ.copy()
    env["PINGODOCK_DEV"] = "1"
    env["PYTHONPATH"] = sysconfig.get_path("purelib")
    # Use o Python base para que terminate() encerre o próprio dock no Windows.
    python = Path(sys._base_executable).with_name("python.exe")
    logs = dock.parent / ".venv" / "dev-data"
    logs.mkdir(parents=True, exist_ok=True)
    child = None
    loaded_source = None
    previous_source = None
    taskbar = WindowsTaskbar()
    taskbar.user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    taskbar.user32.FindWindowW.restype = wintypes.HWND

    with (logs / "reload.log").open("ab", buffering=0) as log:
        try:
            while True:
                if child is not None and child.poll() == 0:
                    return

                try:
                    source = dock.read_bytes()
                except OSError:
                    source = None
                if source is not None and source == previous_source and source != loaded_source:
                    if child is not None and child.poll() is None:
                        child.terminate()
                        child.wait()
                    child = subprocess.Popen(
                        [str(python), str(dock)], cwd=dock.parent, env=env,
                        stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    loaded_source = source
                previous_source = source
                bar = taskbar.user32.FindWindowW(None, "Pingo Dock Dev — Barra superior")
                if child is not None and child.poll() is None and bar:
                    taskbar.hide()
                else:
                    taskbar.restore()
                time.sleep(0.35)
        finally:
            taskbar.restore()
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait()


if __name__ == "__main__":
    main()
