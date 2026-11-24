"""Pingo Dock — um dock elegante para Windows (da família Pingo).

Recursos:
  * ampliação dos ícones ao passar o mouse (efeito "magnification")
  * clique abre o app, ou traz a janela para frente se ele já estiver aberto
  * ícone "pula" ao abrir um app
  * bolinha indicando apps em execução
  * separadores entre grupos de ícones (clique direito → Adicionar separador)
  * arraste ícones para reordenar; arraste para cima e solte para remover
  * arraste arquivos/atalhos/pastas do Explorador para dentro do dock para fixar
  * solte um arquivo em cima de um app para abri-lo com esse app
  * clique direito num app: lista de janelas abertas e "Fechar"
  * Lixeira no fim do dock (soltar arquivos nela apaga; clique direito esvazia)
  * um dock em cada monitor (clique direito → Mostrar em todas as telas)
  * Ctrl+Alt+D mostra/esconde o dock (configurável em "hotkey" no config.json)
  * fundo desfocado e tema claro/escuro acompanhando o Windows
  * aviso de versão nova (pasta/URL configurada em "update_source")
  * menu de clique direito: adicionar app, tamanho, ampliação, ocultar
    automaticamente, iniciar com o Windows, sair
  * some sozinho quando há um app em tela cheia (vídeo, jogo)
"""
from __future__ import annotations

import codecs
import ctypes
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import winreg
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict
from ctypes import wintypes
from pathlib import Path

from tray_host import TrayHost, explorer_taskbar


# Rodando pelo Python "puro" (sem o lançador do .venv, que fica como um processo
# extra na memória): usa as bibliotecas instaladas no .venv do projeto.
_VENV_SITE = Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages"
if _VENV_SITE.is_dir() and str(_VENV_SITE) not in sys.path:
    import site

    site.addsitedir(str(_VENV_SITE))

from PySide6.QtCore import QDateTime, QEvent, QFileInfo, QLocale, QPoint, QPointF, QProcess, QRect, QRectF, QSignalBlocker, QSize, Qt, QTimer
from PySide6.QtNetwork import QNetworkInterface
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFileIconProvider,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSlider,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "PingoDock"     # nome técnico: executável, pastas, registro
APP_TITLE = "Pingo Dock"   # nome que aparece para o usuário
APP_VERSION = "1.4.6"  # o build.py lê daqui a versão do instalador
FROZEN = getattr(sys, "frozen", False)  # rodando como PingoDock.exe (instalado)
APP_DIR = Path(__file__).resolve().parent
DEV_MODE = not FROZEN and os.environ.get("PINGODOCK_DEV") == "1"
TRAY_PREVIEW = FROZEN and "--tray-preview" in sys.argv
SHELL_PROTOTYPE = "--shell-prototype" in sys.argv
SHELL_PREVIEW = SHELL_PROTOTYPE and "--preview" in sys.argv
DESKTOP_MODE = FROZEN or DEV_MODE or SHELL_PROTOTYPE
# Instalado, a pasta do programa pode não ser gravável: configuração e log vão
# para %APPDATA%\PingoDock. Em desenvolvimento, ficam na pasta do projeto.
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME if FROZEN else APP_DIR
if DEV_MODE:
    APP_TITLE = "Pingo Dock Dev"
    APP_VERSION += "-dev"
    DATA_DIR = APP_DIR / ".venv" / "dev-data"
if TRAY_PREVIEW:
    APP_TITLE = "Pingo Dock Prévia"
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PingoDockTrayPreview"
if SHELL_PROTOTYPE:
    APP_TITLE = "Pingo Dock Shell"
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PingoDockShellLab"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"


def configure_ui_font(app: QApplication) -> None:
    families = ["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"]
    font_dir = APP_DIR / "assets" / "fonts" / "poppins"
    for file in sorted(font_dir.glob("Poppins-*.ttf")):
        QFontDatabase.addApplicationFont(str(file))
    if QFontDatabase.hasFamily("Poppins"):
        families.insert(0, "Poppins")
    font = app.font()
    font.setFamilies(families)
    app.setFont(font)

PAD_X = 10          # espaço interno lateral do painel
PAD_TOP = 9         # espaço interno acima dos ícones
PAD_BOTTOM = 12     # espaço interno abaixo dos ícones (onde fica a bolinha)
MARGIN_BOTTOM = 6   # distância do painel até a borda inferior
RADIUS = 2.6        # alcance da ampliação, medido em "ícones"
ICON_RES = 256      # resolução em que os ícones são extraídos (depois reduzidos)
BOUNCE_TIME = 1.0   # duração do pulo ao abrir um app (s)
SEP_SLOT = 6        # largura do "espaço" de um separador (além do espaçamento normal)
LEARN_TIME = 15.0   # por quanto tempo, após abrir um app, tentamos descobrir seu processo real
NET_RETRY_SECS = 10.0  # intervalo entre tentativas de carregar ícones da rede
UPDATE_EVERY_SECS = 6 * 3600  # de quanto em quanto tempo procurar versão nova
HOTKEY_ID = 1
DOCK_FOCUS_HOTKEY_ID = 2
DOCK_FOCUS_SHORTCUT = "Ctrl+Alt+K"

# Lançadores cujo processo real tem outro nome.
KNOWN_ALIASES = {
    "calc.exe": {"calculatorapp.exe"},
    "wt.exe": {"windowsterminal.exe"},
}
# Processos que nunca devem ser "aprendidos" como sendo de um app.
NEVER_LEARN = {"explorer.exe", "applicationframehost.exe", "python.exe", "pythonw.exe",
               "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe"}

ICON_SIZES = {"Pequeno": 44, "Médio": 56, "Grande": 72}

# --------------------------------------------------------------------------
# Win32 (via ctypes) — achar/ativar janelas e detectar tela cheia
# --------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

HWND = wintypes.HWND
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]

class MONITORINFOEX(MONITORINFO):
    _fields_ = [("device", wintypes.WCHAR * 32)]


def _proto(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = list(argtypes)


_proto(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_proto(user32.EnumChildWindows, wintypes.BOOL, HWND, WNDENUMPROC, wintypes.LPARAM)
_proto(user32.GetWindowTextW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_proto(user32.GetWindowThreadProcessId, wintypes.DWORD, HWND, ctypes.POINTER(wintypes.DWORD))
_proto(user32.GetLastActivePopup, HWND, HWND)
_proto(user32.IsWindowVisible, wintypes.BOOL, HWND)
_proto(user32.IsIconic, wintypes.BOOL, HWND)
_proto(user32.IsZoomed, wintypes.BOOL, HWND)
_proto(user32.GetWindow, HWND, HWND, wintypes.UINT)
_proto(user32.GetWindowLongPtrW, ctypes.c_ssize_t, HWND, ctypes.c_int)
_proto(user32.SetWindowLongPtrW, ctypes.c_ssize_t, HWND, ctypes.c_int, ctypes.c_ssize_t)
_proto(user32.SetLayeredWindowAttributes, wintypes.BOOL, HWND, wintypes.DWORD,
       wintypes.BYTE, wintypes.DWORD)
_proto(user32.GetLayeredWindowAttributes, wintypes.BOOL, HWND, ctypes.POINTER(wintypes.DWORD),
       ctypes.POINTER(wintypes.BYTE), ctypes.POINTER(wintypes.DWORD))
_proto(user32.GetWindowTextLengthW, ctypes.c_int, HWND)
_proto(user32.GetClassNameW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_proto(user32.ShowWindow, wintypes.BOOL, HWND, ctypes.c_int)
_proto(user32.SetForegroundWindow, wintypes.BOOL, HWND)
_proto(user32.GetForegroundWindow, HWND)
_proto(user32.GetWindowRect, wintypes.BOOL, HWND, ctypes.POINTER(wintypes.RECT))
_proto(user32.GetClientRect, wintypes.BOOL, HWND, ctypes.POINTER(wintypes.RECT))
_proto(user32.MonitorFromWindow, wintypes.HMONITOR, HWND, wintypes.DWORD)
_proto(user32.GetMonitorInfoW, wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
_proto(user32.keybd_event, None, wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t)
_proto(dwmapi.DwmGetWindowAttribute, ctypes.c_long, HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
_proto(kernel32.CreateMutexW, wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)

GW_OWNER = 4
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
SW_MAXIMIZE, SW_RESTORE = 3, 9
VK_MENU = 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
START_KEY_TAG = 0x50494E47
KEYEVENTF_KEYUP = 0x2
MONITOR_DEFAULTTONEAREST = 2

SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


shell32 = ctypes.WinDLL("shell32")
gdi32 = ctypes.WinDLL("gdi32")

WM_CLOSE = 0x0010
WM_HOTKEY = 0x0312
_proto(user32.PostMessageW, wintypes.BOOL, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_proto(user32.SendMessageTimeoutW, wintypes.LPARAM, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
_proto(user32.FindWindowW, HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
_proto(user32.FindWindowExW, HWND, HWND, HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
_proto(user32.GetDlgItem, HWND, HWND, ctypes.c_int)
_proto(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_proto(user32.WindowFromPoint, HWND, wintypes.POINT)
_proto(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
_proto(user32.ShowWindowAsync, wintypes.BOOL, HWND, ctypes.c_int)


class KeyboardHookEvent(ctypes.Structure):
    _fields_ = [("vk_code", wintypes.DWORD), ("scan_code", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("extra_info", ctypes.c_size_t)]


KEYBOARD_HOOK = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_proto(user32.SetWindowsHookExW, wintypes.HANDLE, ctypes.c_int, KEYBOARD_HOOK, wintypes.HINSTANCE, wintypes.DWORD)
_proto(user32.UnhookWindowsHookEx, wintypes.BOOL, wintypes.HANDLE)
_proto(user32.CallNextHookEx, wintypes.LPARAM, wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class StartKeyHook:
    def __init__(self, group):
        self.group = group
        self.pressed = set()
        self.used_in_shortcut = False
        self.callback = KEYBOARD_HOOK(self.handle_key)
        self.hook = user32.SetWindowsHookExW(13, self.callback, None, 0)  # WH_KEYBOARD_LL
        if not self.hook:
            log("não foi possível configurar a tecla Windows para abrir o Iniciar")

    def close(self):
        if self.hook:
            user32.UnhookWindowsHookEx(self.hook)
            self.hook = None

    def open_start(self):
        dock = self.group.leader
        if dock is not None:
            dock.toggle_start_launcher()

    def handle_key(self, code, message, data):
        if code >= 0:
            event = KeyboardHookEvent.from_address(data)
            if event.extra_info != START_KEY_TAG:
                key = event.vk_code
                released = bool(event.flags & 0x80)  # LLKHF_UP
                if key in (VK_LWIN, VK_RWIN):
                    if not released:
                        if not self.pressed:
                            self.used_in_shortcut = any(user32.GetAsyncKeyState(modifier) & 0x8000
                                                       for modifier in (0x10, 0x11, VK_MENU))
                        elif key not in self.pressed:
                            self.used_in_shortcut = True
                        self.pressed.add(key)
                    elif key in self.pressed:
                        self.pressed.remove(key)
                        if not self.pressed and not self.used_in_shortcut:
                            # O Windows já recebeu o pressionamento. Mascaramos o menu nativo
                            # antes de reenviar a soltura, sem interferir em atalhos Win+tecla.
                            user32.keybd_event(0xE8, 0, 0, START_KEY_TAG)
                            user32.keybd_event(0xE8, 0, KEYEVENTF_KEYUP, START_KEY_TAG)
                            user32.keybd_event(key, event.scan_code, KEYEVENTF_KEYUP | 0x01, START_KEY_TAG)
                            QTimer.singleShot(0, self.open_start)
                            return 1
                elif self.pressed and not released:
                    self.used_in_shortcut = True
        return user32.CallNextHookEx(self.hook, code, message, data)
_proto(user32.RegisterHotKey, wintypes.BOOL, HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_proto(user32.UnregisterHotKey, wintypes.BOOL, HWND, ctypes.c_int)
_proto(user32.SetWindowRgn, ctypes.c_int, HWND, wintypes.HRGN, wintypes.BOOL)
_proto(gdi32.DeleteObject, wintypes.BOOL, wintypes.HANDLE)
_proto(user32.SetWindowPos, wintypes.BOOL, HWND, HWND, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, ctypes.c_int, wintypes.UINT)
_proto(gdi32.CreateRoundRectRgn, wintypes.HRGN, ctypes.c_int, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, ctypes.c_int, ctypes.c_int)


def close_window(hwnd) -> None:
    """Pede para a janela fechar (como clicar no X: o app pode perguntar se quer salvar)."""
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


# ---- atalho de teclado global ----------------------------------------------
MODIFIERS = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """'Ctrl+Alt+D' → (modificadores, tecla virtual). Aceita letras, números e F1–F12."""
    mods, key = 0, 0
    for part in (p.strip().lower() for p in text.split("+") if p.strip()):
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 12:
            key = 0x70 + int(part[1:]) - 1
        else:
            return None
    return (mods, key) if key and mods else None


# ---- Lixeira ---------------------------------------------------------------
class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]


class SHSTOCKICONINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hIcon", wintypes.HICON), ("iSysImageIndex", ctypes.c_int),
                ("iIcon", ctypes.c_int), ("szPath", wintypes.WCHAR * 260)]


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR)]


_proto(shell32.SHQueryRecycleBinW, ctypes.c_long, wintypes.LPCWSTR, ctypes.POINTER(SHQUERYRBINFO))
_proto(shell32.SHEmptyRecycleBinW, ctypes.c_long, HWND, wintypes.LPCWSTR, wintypes.DWORD)
_proto(shell32.SHGetStockIconInfo, ctypes.c_long, ctypes.c_int, wintypes.UINT, ctypes.POINTER(SHSTOCKICONINFO))
_proto(shell32.SHFileOperationW, ctypes.c_int, ctypes.POINTER(SHFILEOPSTRUCTW))

SIID_RECYCLER, SIID_RECYCLERFULL = 31, 32
FO_DELETE, FOF_ALLOWUNDO = 3, 0x40


def recycle_bin_count() -> int:
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    if shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0
    return info.i64NumItems


def stock_icon_location(siid: int) -> tuple[str, int]:
    info = SHSTOCKICONINFO()
    info.cbSize = ctypes.sizeof(SHSTOCKICONINFO)
    if shell32.SHGetStockIconInfo(siid, 0, ctypes.byref(info)) != 0:  # SHGSI_ICONLOCATION
        return "", 0
    return info.szPath, info.iIcon


def send_to_recycle_bin(paths: list[str]) -> None:
    """Move para a Lixeira com a janela de confirmação/progresso do próprio Windows
    (dá para desfazer). Roda numa thread para não travar o dock."""
    def work() -> None:
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = "\0".join(os.path.normpath(p) for p in paths) + "\0\0"
        op.fFlags = FOF_ALLOWUNDO
        shell32.SHFileOperationW(ctypes.byref(op))

    threading.Thread(target=work, daemon=True).start()


def empty_recycle_bin() -> None:
    """Esvazia a Lixeira; o Windows pede confirmação antes."""
    threading.Thread(target=lambda: shell32.SHEmptyRecycleBinW(None, None, 0), daemon=True).start()


def open_recycle_bin() -> None:
    os.startfile("explorer.exe", arguments="shell:RecycleBinFolder")


# ---- fundo desfocado (DWM) -------------------------------------------------
class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


_proto(user32.SetWindowCompositionAttribute, wintypes.BOOL, HWND, ctypes.POINTER(WINCOMPATTRDATA))
WCA_ACCENT_POLICY, ACCENT_ENABLE_BLURBEHIND = 19, 3


def enable_blur(hwnd) -> bool:
    accent = ACCENT_POLICY(ACCENT_ENABLE_BLURBEHIND, 0, 0, 0)
    data = WINCOMPATTRDATA(WCA_ACCENT_POLICY, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                           ctypes.sizeof(accent))
    return bool(user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))


def windows_uses_light_theme() -> bool:
    """Tema da barra de tarefas/Iniciar (o que o dock imita)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except OSError:
        return False


def window_class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_proto(kernel32.GetApplicationUserModelId, ctypes.c_long, wintypes.HANDLE,
       ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR)
_proto(kernel32.OpenProcess, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_proto(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
_proto(kernel32.QueryFullProcessImageNameW, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
       wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
_proto(kernel32.GetCurrentProcess, wintypes.HANDLE)
_proto(kernel32.SetProcessWorkingSetSize, wintypes.BOOL, wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t)


def process_image_path(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def packaged_launch_uri(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        length = wintypes.UINT()
        kernel32.GetApplicationUserModelId(handle, ctypes.byref(length), None)
        if not length.value:
            return ""
        value = ctypes.create_unicode_buffer(length.value)
        result = kernel32.GetApplicationUserModelId(handle, ctypes.byref(length), value)
        return "shell:AppsFolder\\" + value.value if result == 0 and "!" in value.value else ""
    finally:
        kernel32.CloseHandle(handle)


def process_name(pid: int) -> str:
    return os.path.basename(process_image_path(pid)).lower()


def trim_memory() -> None:
    """Devolve ao Windows as páginas de memória que não estão em uso (as que só
    serviram na inicialização, p.ex.). O que for usado de novo volta sozinho."""
    kernel32.SetProcessWorkingSetSize(kernel32.GetCurrentProcess(), ctypes.c_size_t(-1), ctypes.c_size_t(-1))


def is_app_window(hwnd) -> bool:
    """Janela 'de verdade' de um app (a que apareceria na barra de tarefas)."""
    if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
        return False
    if user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
        return False
    if user32.GetWindowTextLengthW(hwnd) == 0 or window_class(hwnd) in SHELL_CLASSES:
        return False
    cloaked = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return cloaked.value == 0


def _uwp_child_pid(frame, frame_pid: int) -> int | None:
    """Apps da Store moram dentro de uma moldura do ApplicationFrameHost; o app
    de verdade é dono de uma janela filha."""
    found: list[int] = []

    def callback(hwnd, _lparam):
        pid = window_pid(hwnd)
        if pid != frame_pid:
            found.append(pid)
            return False
        return True

    user32.EnumChildWindows(frame, WNDENUMPROC(callback), 0)
    return found[0] if found else None


def app_windows() -> list[tuple[int, int]]:
    """(janela, pid do processo real) de todas as janelas de app, da mais à frente
    para a mais atrás. Para apps da Store, o pid é o do app, não o da moldura."""
    tops: list[tuple[int, int, str]] = []
    detached: dict[str, int] = {}

    def callback(hwnd, _lparam):
        cls = window_class(hwnd)
        if cls == "Windows.UI.Core.CoreWindow":
            # App da Store minimizado: a janela interna se solta da moldura.
            detached.setdefault(window_title(hwnd), window_pid(hwnd))
        elif is_app_window(hwnd):
            tops.append((hwnd, window_pid(hwnd), cls))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)

    result = []
    for hwnd, pid, cls in tops:
        if cls == "ApplicationFrameWindow":
            pid = _uwp_child_pid(hwnd, pid) or detached.get(window_title(hwnd), pid)
        result.append((hwnd, pid))
    return result



def bring_to_front(hwnd) -> None:
    if window_class(hwnd) == "TApplication":
        # O instalador Delphi/Inno restaura o assistente pelo comando da barra.
        if user32.IsIconic(hwnd) and user32.PostMessageW(hwnd, 0x0112, 0xF120, 0):
            return  # WM_SYSCOMMAND / SC_RESTORE
        popup = user32.GetLastActivePopup(hwnd)
        if popup and popup != hwnd and user32.IsWindowVisible(popup):
            hwnd = popup
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if not user32.SetForegroundWindow(hwnd):
        # O Windows às vezes recusa; um toque "falso" no Alt libera a troca de foco.
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)


def _covers(r: wintypes.RECT, m: wintypes.RECT, tol: int = 0) -> bool:
    return (r.left <= m.left + tol and r.top <= m.top + tol
            and r.right >= m.right - tol and r.bottom >= m.bottom - tol)


WS_EX_TOPMOST = 0x00000008
WS_EX_LAYERED = 0x00080000

# Ferramentas de captura de tela: a camada de seleção delas cobre a tela toda,
# mas não é um "app em tela cheia" — o dock precisa continuar aparecendo no print.
SCREENSHOT_TOOLS = {
    "snippingtool.exe", "screenclippinghost.exe", "screensketch.exe", "lightshot.exe",
    "sharex.exe", "greenshot.exe", "picpick.exe", "flameshot.exe", "ksnip.exe",
    "snagit32.exe", "snagiteditor.exe", "screenpresso.exe", "gyazo.exe",
    "gamebar.exe", "gamebarftserver.exe", "nvidia share.exe",
}


# Títulos de janelas de captura de tela de apps que também têm uma janela normal
# (não dá para ignorar o processo inteiro).
OVERLAY_TITLES = {
    "ScreenshotWindow",  # Adobe Photoshop Express (PSExpressCore.exe)
}


def is_overlay(hwnd, pid: int) -> bool:
    """Camadas temporárias por cima de tudo (captura de tela, seletores etc.).
    Vídeos e jogos em tela cheia não ficam 'sempre por cima'; essas camadas sim."""
    if user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST:
        return True
    if window_title(hwnd) in OVERLAY_TITLES:
        return True
    return process_name(pid) in SCREENSHOT_TOOLS


def cover_state(own_hwnd: int) -> tuple[str | None, int | None]:
    """O que está ocupando o monitor do dock e qual janela causou isso:
    "fullscreen" (vídeo, jogo, F11), "maximized" (janela maximizada) ou None."""
    monitor = user32.MonitorFromWindow(own_hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    own_pid = os.getpid()

    # Só janelas de app "de verdade" (as que aparecem na barra de tarefas).
    # Camadas temporárias — seleção de print, menus, notificações — ficam de fora.
    candidates: list[int] = []

    def callback(hwnd, _lparam):
        if is_app_window(hwnd):
            candidates.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)

    state, cause = None, None
    for hwnd in candidates:
        pid = window_pid(hwnd)
        if hwnd == own_hwnd or pid == own_pid or user32.IsIconic(hwnd):
            continue
        if is_overlay(hwnd, pid):
            continue
        if user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST) != monitor:
            continue
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        zoomed = bool(user32.IsZoomed(hwnd))
        has_caption = user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION == WS_CAPTION
        covers_monitor = _covers(rect, info.rcMonitor) and not zoomed and not has_caption
        # Janela transparente (layered) sem borda cobrindo a tela = camada de seleção
        # de print ou efeito visual; vídeos e jogos em tela cheia são opacos.
        if covers_monitor and user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_LAYERED:
            continue
        if covers_monitor:
            return "fullscreen", hwnd
        if state is None and (zoomed or _covers(rect, info.rcWork, tol=2)):
            state, cause = "maximized", hwnd
    return state, cause


# --------------------------------------------------------------------------
# Registro de diagnóstico (dock.log) — para investigar falhas
# --------------------------------------------------------------------------
LOG_PATH = DATA_DIR / "dock.log"


def describe_window(hwnd) -> str:
    if not hwnd:
        return "-"
    pid = window_pid(hwnd)
    name = process_name(pid) or "?"
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    return (f"{name} pid={pid} class={window_class(hwnd)!r} title={window_title(hwnd)[:60]!r} "
            f"rect=({rect.left},{rect.top},{rect.right},{rect.bottom}) "
            f"style={style & 0xFFFFFFFF:#010x} exstyle={ex & 0xFFFFFFFF:#010x}")


def log(message: str) -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 256_000:
            LOG_PATH.unlink()
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except OSError:
        pass


_instance_mutex = None


def already_running() -> bool:
    global _instance_mutex
    name = "PingoDockDev" if DEV_MODE else APP_NAME
    if SHELL_PROTOTYPE:
        name = "PingoDockShellLab"
    _instance_mutex = kernel32.CreateMutexW(None, False, f"Local\\{name}_SingleInstance")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


# --------------------------------------------------------------------------
# Iniciar com o Windows (registro do usuário, sem precisar de admin)
# --------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command() -> str:
    if FROZEN:
        return f'"{sys.executable}"'
    # Python "puro" (não o lançador do .venv) → um processo só.
    base = getattr(sys, "_base_executable", None) or sys.executable
    pythonw = Path(base).with_name("pythonw.exe")
    return f'"{pythonw}" "{Path(__file__).resolve()}"'


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


LEGACY_NAME = "MacDock"  # nome antigo do produto


def migrate_from_macdock() -> None:
    """Quem usava o MacDock continua com os mesmos apps e opções no Pingo Dock:
    copia a configuração antiga e troca a entrada de "Iniciar com o Windows"."""
    import shutil

    if FROZEN and not CONFIG_PATH.exists():
        old_dir = Path(os.environ.get("APPDATA", str(Path.home()))) / LEGACY_NAME
        for name in ("config.json", "dock.log"):
            if (old_dir / name).exists():
                try:
                    shutil.copy2(old_dir / name, DATA_DIR / name)
                except OSError:
                    pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            winreg.QueryValueEx(key, LEGACY_NAME)  # existe a entrada do MacDock?
            winreg.DeleteValue(key, LEGACY_NAME)
        set_startup(True)  # mesma escolha do usuário, agora com o nome novo
    except OSError:
        pass


# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "top_bar_style": "transparent",
    "icon_size": 56,
    "magnification": 1.85,
    "magnify": True,
    "spacing": 8,
    "autohide": False,
    "all_screens": False,   # um dock em cada monitor
    "theme": "auto",        # "auto" (segue o Windows), "dark" ou "light"
    "blur": True,           # fundo desfocado atrás do painel
    "hotkey": "Ctrl+Alt+F9" if DEV_MODE else "Ctrl+Alt+D",  # mostrar/esconder o dock
    "update_source": "",    # pasta ou URL com o latest.json (vazio = não procura)
    "apps": [],
}


def default_apps() -> list[dict]:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        ("Explorador de Arquivos", rf"{windir}\explorer.exe"),
        ("Microsoft Edge", rf"{pf86}\Microsoft\Edge\Application\msedge.exe"),
        ("Google Chrome", rf"{pf}\Google\Chrome\Application\chrome.exe"),
        ("Firefox", rf"{pf}\Mozilla Firefox\firefox.exe"),
        ("Visual Studio Code", rf"{local}\Programs\Microsoft VS Code\Code.exe"),
        ("Bloco de Notas", rf"{windir}\System32\notepad.exe"),
        ("Calculadora", rf"{windir}\System32\calc.exe"),
        ("PowerShell", rf"{windir}\System32\WindowsPowerShell\v1.0\powershell.exe"),
        ("Painel de Controle", rf"{windir}\System32\control.exe"),
    ]
    return [{"name": n, "path": p} for n, p in candidates if os.path.exists(p)]


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            cfg.setdefault("welcome_seen", True)
            return cfg
        except (OSError, ValueError):
            pass
    cfg["apps"] = default_apps()
    cfg["welcome_seen"] = False
    save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# Atualizações: o instalador publicado numa pasta (ex.: \\servidor\TI\PingoDock)
# junto com um latest.json  {"version": "1.2.0", "setup": "PingoDock-Setup-1.3.0.exe"}
# --------------------------------------------------------------------------
def update_source(cfg: dict) -> str:
    """Onde procurar versão nova: config.json ou, na versão instalada, o
    update_source.txt que o build.py coloca ao lado do PingoDock.exe."""
    if cfg.get("update_source"):
        return cfg["update_source"].strip()
    if FROZEN:
        baked = Path(sys.executable).with_name("update_source.txt")
        try:
            return baked.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


def version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _join_source(source: str, name: str) -> str:
    if source.lower().startswith(("http://", "https://")):
        return source.rstrip("/") + "/" + name
    return os.path.join(source, name)


def fetch_latest(source: str) -> dict | None:
    """Lê o latest.json. Devolve {"version", "setup"} com o caminho/URL completo
    do instalador, ou None. Roda numa thread (rede lenta não trava o dock)."""
    manifest = source if source.lower().endswith(".json") else _join_source(source, "latest.json")
    base = manifest.rsplit("/", 1)[0] if manifest.lower().startswith("http") else os.path.dirname(manifest)
    try:
        if manifest.lower().startswith(("http://", "https://")):
            import urllib.request

            with urllib.request.urlopen(manifest, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        else:
            data = json.loads(Path(manifest).read_text(encoding="utf-8-sig"))
        version, setup = str(data["version"]), str(data["setup"])
    except Exception as exc:  # rede fora, arquivo ausente, JSON inválido...
        log(f"atualização: não foi possível ler {manifest}: {exc}")
        return None
    if not setup.lower().startswith(("http://", "https://")) and not os.path.isabs(setup):
        setup = _join_source(base, setup)
    return {"version": version, "setup": setup}


def run_update_installer(setup: str) -> None:
    """Baixa (se for URL) e roda o instalador em modo silencioso; ele fecha o dock,
    atualiza e abre o dock de novo (parâmetro /UPDATE no pingodock.iss)."""
    def work() -> None:
        path = setup
        try:
            if setup.lower().startswith(("http://", "https://")):
                import tempfile
                import urllib.request

                path = os.path.join(tempfile.gettempdir(), setup.rsplit("/", 1)[-1] or "PingoDock-Setup.exe")
                urllib.request.urlretrieve(setup, path)
            os.startfile(path, arguments="/SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE")
        except Exception as exc:
            log(f"atualização: falha ao executar {setup}: {exc}")

    threading.Thread(target=work, daemon=True).start()


# --------------------------------------------------------------------------
# Item do dock
# --------------------------------------------------------------------------
def darkened(pm: QPixmap) -> QPixmap:
    out = QPixmap(pm)
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
    p.fillRect(out.rect(), QColor(0, 0, 0, 100))
    p.end()
    return out


def read_shortcut(path: str) -> tuple[str, str, int]:
    """Lê um atalho .lnk e devolve (alvo, arquivo do ícone, índice do ícone)."""
    info = parse_shortcut(path)
    return info["target"], info["icon_file"], info["icon_index"]


def parse_shortcut(path: str) -> dict:
    """Lê um atalho .lnk: alvo, argumentos, pasta de trabalho e ícone.
    Segue o formato MS-SHLLINK; campos ausentes voltam vazios."""
    result = {"target": "", "args": "", "workdir": "", "icon_file": "", "icon_index": 0}
    target, icon_file, icon_index, strings = _parse_shortcut_raw(path)
    result.update(target=target, icon_file=icon_file, icon_index=icon_index)
    if strings:
        result["workdir"] = os.path.expandvars(strings[2])
        result["args"] = strings[3]
    return result


def _parse_shortcut_raw(path: str) -> tuple[str, str, int, list[str]]:
    import struct

    target, icon_file, icon_index, strings = "", "", 0, []
    try:
        data = Path(path).read_bytes()
        flags, = struct.unpack_from("<I", data, 20)
        icon_index, = struct.unpack_from("<i", data, 56)
        unicode = bool(flags & 0x80)
        pos = 76
        if flags & 0x01:  # HasLinkTargetIDList
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        if flags & 0x02:  # HasLinkInfo
            info_size, _hdr, _flags, _vol, base_off = struct.unpack_from("<5I", data, pos)
            if _flags & 1 and base_off:  # VolumeIDAndLocalBasePath
                end = data.index(b"\0", pos + base_off)
                target = data[pos + base_off:end].decode("mbcs", "replace")
            pos += info_size
        strings = []
        for bit in (0x04, 0x08, 0x10, 0x20, 0x40):  # Name, RelPath, WorkDir, Args, IconLocation
            if flags & bit:
                n, = struct.unpack_from("<H", data, pos)
                pos += 2
                size = n * 2 if unicode else n
                raw = data[pos:pos + size]
                strings.append(raw.decode("utf-16-le" if unicode else "mbcs", "replace"))
                pos += size
            else:
                strings.append("")
        icon_file = strings[4]
        # Blocos extras: caminhos com variáveis de ambiente (%ProgramFiles% etc.)
        while pos + 8 <= len(data):
            size, sig = struct.unpack_from("<II", data, pos)
            if size < 8:
                break
            if sig in (0xA0000001, 0xA0000007) and size >= 788:
                value = data[pos + 268:pos + 788].decode("utf-16-le", "replace").split("\0")[0]
                if sig == 0xA0000001 and not target:
                    target = value
                elif sig == 0xA0000007:
                    icon_file = value
            pos += size
    except (OSError, ValueError, struct.error):
        return "", "", 0, []
    return os.path.expandvars(target), os.path.expandvars(icon_file), icon_index, strings


_proto(user32.PrivateExtractIconsW, wintypes.UINT, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
       wintypes.UINT, wintypes.UINT)
_proto(user32.DestroyIcon, wintypes.BOOL, wintypes.HICON)

ICON_FILE_EXTS = (".exe", ".dll", ".ico", ".icl", ".cpl", ".scr")


def installer_executable(path: str) -> bool:
    name = Path(path).stem.casefold()
    return "setup" in name or "install" in name or name == "msiexec"


def installer_progress(hwnd) -> float | None:
    title = window_title(hwnd)
    match = re.search(r"(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*%", title)
    if match:
        percent = float(match[1].replace(",", "."))
        if 0 <= percent <= 100:
            return percent

    def read_control(control, message, flag=0):
        result = ctypes.c_size_t()
        # PBM_GETRANGE com lParam nulo retorna um limite sem ponteiros remotos.
        if user32.SendMessageTimeoutW(control, message, flag, 0, 0x0002, 30, ctypes.byref(result)):
            return ctypes.c_int(result.value).value
        return None

    progress = None

    def visit(child, _lparam):
        nonlocal progress
        if (window_class(child).casefold() != "msctls_progress32" or not user32.IsWindowVisible(child)
                or user32.GetWindowLongPtrW(child, GWL_STYLE) & 0x08):  # PBS_MARQUEE
            return True
        low = read_control(child, 0x0407, 1)  # PBM_GETRANGE
        high = read_control(child, 0x0407)
        position = read_control(child, 0x0408)  # PBM_GETPOS
        if low is not None and high is not None and position is not None and low <= position <= high and high > low:
            progress = 100 * (position - low) / (high - low)
            return False
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(visit), 0)
    return progress


def extract_icon_image(file: str, index: int, size: int) -> QImage:
    hicon = wintypes.HICON()
    icon_id = wintypes.UINT()
    count = user32.PrivateExtractIconsW(file, index, size, size, ctypes.byref(hicon),
                                        ctypes.byref(icon_id), 1, 0)
    if count in (0, 0xFFFFFFFF) or not hicon:
        return QImage()
    try:
        return QImage.fromHICON(hicon.value)
    finally:
        user32.DestroyIcon(hicon)


def extract_icon(file: str, index: int = 0, size: int = ICON_RES) -> QPixmap:
    """Ícone em alta resolução direto do .exe/.dll/.ico (sem a seta de atalho)."""
    return QPixmap.fromImage(extract_icon_image(file, index, size))


def points_to_folder(path: str, target: str) -> bool:
    """Se o item é uma pasta. Para atalhos, lê o atributo gravado no próprio .lnk,
    sem tocar na rede (pastas de servidor offline travariam o dock)."""
    if path.lower().endswith(".lnk"):
        try:
            with open(path, "rb") as f:
                header = f.read(28)
            return len(header) == 28 and bool(int.from_bytes(header[24:28], "little") & 0x10)
        except OSError:
            return False
    if path.startswith("\\\\"):
        return not Path(path).suffix
    return os.path.isdir(path)


def clean_name(name: str) -> str:
    for suffix in (" - Atalho", " - Shortcut", " - atalho"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


WEB_SCHEMES = ("http:", "https:", "ftp:", "file:", "mailto:")


def is_web_link(path: str) -> bool:
    """Atalho .url para um site (abre no navegador). Diferente de steam:// etc.,
    não é um 'app' com janela própria."""
    if not path.lower().endswith(".url"):
        return False
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().upper().startswith("URL="):
                return line.strip()[4:].lower().startswith(WEB_SCHEMES)
    except OSError:
        pass
    return False


def read_url_shortcut(path: str) -> tuple[str, int]:
    """Atalho de internet (.url, ex.: jogos/apps da Steam): (arquivo do ícone, índice)."""
    import configparser

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    for encoding in ("utf-8", "mbcs"):
        try:
            parser.read(path, encoding=encoding)
            break
        except (UnicodeDecodeError, configparser.Error):
            continue
    if not parser.has_section("InternetShortcut"):
        return "", 0
    section = parser["InternetShortcut"]
    try:
        index = int(section.get("IconIndex", "0"))
    except ValueError:
        index = 0
    return os.path.expandvars(section.get("IconFile", "")), index


def is_network(path: str) -> bool:
    return path.startswith(("\\\\", "//"))


def shell_item_icon_image(target: str) -> QImage:
    pidl = ctypes.c_void_p()
    info = ShellFileInfo()
    if shell32.SHParseDisplayName(target, None, ctypes.byref(pidl), 0, None) == 0:
        try:
            flags = 0x100 | 0x08  # SHGFI_ICON | SHGFI_PIDL
            shell32.SHGetFileInfoW(pidl, 0, ctypes.byref(info), ctypes.sizeof(info), flags)
            if info.icon:
                try:
                    return QImage.fromHICON(info.icon)
                finally:
                    user32.DestroyIcon(info.icon)
        finally:
            ole32.CoTaskMemFree(pidl)
    return QImage()


def packaged_icon_image(executable: str, launch_uri: str, px: int) -> QImage:
    app_id = launch_uri.rpartition("!")[2]
    for directory in Path(executable).parents:
        if directory.name.casefold() == "windowsapps":
            break
        manifest = directory / "AppxManifest.xml"
        try:
            if not manifest.is_file():
                continue
            applications = ET.parse(manifest).findall("./{*}Applications/{*}Application")
            for app in applications:
                if app.get("Id") != app_id:
                    continue
                visual = app.find("{*}VisualElements")
                if visual is None:
                    break
                candidates = []
                for attribute, base_size in (("Square44x44Logo", 44), ("Square150x150Logo", 150)):
                    if not (relative := visual.get(attribute)):
                        continue
                    logo = directory / relative
                    for file in logo.parent.glob(f"{logo.stem}*{logo.suffix}"):
                        qualifiers = file.stem[len(logo.stem):].casefold()
                        if qualifiers and not qualifiers.startswith((".", "_")):
                            continue
                        size = re.search(r"targetsize-(\d+)", qualifiers)
                        scale = re.search(r"scale-(\d+)", qualifiers)
                        estimated_px = (int(size[1]) if size else
                                        base_size * int(scale[1]) // 100 if scale else base_size)
                        unplated = "_altform-unplated" in qualifiers
                        candidates.append((estimated_px < px, not unplated,
                                           abs(estimated_px - px), file))
                for _, _, _, file in sorted(candidates, key=lambda value: value[:3]):
                    if not (image := QImage(str(file))).isNull():
                        return image
                break
        except (OSError, ET.ParseError):
            return QImage()
        break
    return QImage()


def load_icon(path: str, target: str, provider: QFileIconProvider, px: int,
              is_folder: bool = False, launch_uri: str = "") -> tuple[QPixmap, list[str]]:
    """Ícone com no máximo `px` pixels de lado: extrai em alta resolução e reduz
    com suavização para o tamanho máximo em que ele aparece na tela (economiza
    memória sem perder nitidez).
    Devolve também os caminhos de rede que estavam inacessíveis (ex.: o dock
    abriu no logon antes da rede ficar pronta) — o dock tenta de novo depois."""
    missing: list[str] = []
    pm = QPixmap()
    if launch_uri:
        image = packaged_icon_image(target, launch_uri, px)
        if image.isNull():
            image = shell_item_icon_image(launch_uri)
        pm = QPixmap.fromImage(image)
    if pm.isNull():
        pm = _load_icon_full(path, target, provider, is_folder, missing)
    if pm.width() > px or pm.height() > px:
        pm = pm.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    return pm, missing


def _load_icon_full(path: str, target: str, provider: QFileIconProvider,
                    is_folder: bool, missing: list[str]) -> QPixmap:
    sources: list[tuple[str, int]] = []
    if path.lower().endswith(".lnk"):
        _, icon_file, icon_index = read_shortcut(path)
        if icon_file:
            sources.append((icon_file, icon_index))
    elif path.lower().endswith(".url"):
        icon_file, icon_index = read_url_shortcut(path)
        if icon_file:
            sources.append((icon_file, icon_index))
    if (target.lower().endswith(ICON_FILE_EXTS)
            or target.lower().endswith(".tmp") and installer_executable(target)):
        sources.append((target, 0))
    for file, index in sources:
        if os.path.exists(file):
            pm = extract_icon(file, index)
            if not pm.isNull():
                return pm
        elif is_network(file):
            missing.append(file)
    # Pastas, documentos etc.: ícone do sistema (do alvo, para não vir com a seta).
    target_ok = bool(target) and os.path.exists(target)
    if target and not target_ok and is_network(target):
        missing.append(target)
    if not target_ok and is_folder:
        # Pasta de rede fora do ar: pasta genérica em vez do .lnk com a seta.
        pm = provider.icon(QFileIconProvider.IconType.Folder).pixmap(QSize(ICON_RES, ICON_RES))
        if not pm.isNull():
            return pm
    info = QFileInfo(target if target_ok else path)
    pm = provider.icon(info).pixmap(QSize(ICON_RES, ICON_RES))
    if pm.isNull():
        pm = provider.icon(QFileIconProvider.IconType.File).pixmap(QSize(ICON_RES, ICON_RES))
    return pm


class DockItem:
    is_sep = False
    is_trash = False
    transient = False
    fixed = False

    def __init__(self, name: str, path: str, provider: QFileIconProvider, icon_px: int,
                 match: list[str] | None = None, launch_uri: str = "") -> None:
        self.name = name
        self.path = path
        self.launch_uri = launch_uri
        target = path
        if path.lower().endswith(".lnk"):
            target = QFileInfo(path).symLinkTarget() or read_shortcut(path)[0]
        self.target = target
        exe = os.path.basename(target).lower() if target and target.lower().endswith(".exe") else ""
        # Links de sites abrem no navegador: não têm "processo próprio", então
        # nunca ganham bolinha nem aprendem processo (senão virariam o Chrome).
        self.is_web = is_web_link(path)
        # Nomes de processo que contam como "este app está aberto".
        self.learned = set() if self.is_web else {m.lower() for m in (match or [])}
        self.match_names = ({exe} if exe else set()) | KNOWN_ALIASES.get(exe, set()) | self.learned
        self.is_folder = points_to_folder(path, target)
        self.pixmap, self.icon_missing = load_icon(path, target, provider, icon_px,
                                                   self.is_folder, self.launch_uri)
        self.running = False
        self.installer = installer_executable(target)
        self.progress: float | None = None
        self.bounce_t0: float | None = None
        # Enquanto não sabemos qual processo o app cria, observamos a primeira
        # janela nova que aparecer em primeiro plano após abri-lo.
        self.learn_t0: float | None = None
        self.learn_before: set[int] = set()

    def open_with_command(self) -> tuple[str, str, str] | None:
        """(programa, argumentos fixos, pasta de trabalho) para abrir arquivos
        soltos em cima deste item — só para apps (.exe ou atalho para .exe)."""
        if not hasattr(self, "_open_with"):  # lido uma vez (é consultado a cada movimento do mouse)
            self._open_with = None
            if self.is_web or self.is_folder:
                pass
            elif self.path.lower().endswith(".exe"):
                self._open_with = (self.path, "", os.path.dirname(self.path))
            elif self.path.lower().endswith(".lnk") and self.target.lower().endswith(".exe"):
                info = parse_shortcut(self.path)
                self._open_with = (self.target, info["args"],
                                   info["workdir"] or os.path.dirname(self.target))
        return self._open_with

    def reload_icon(self, provider: QFileIconProvider, icon_px: int) -> None:
        self.pixmap, self.icon_missing = load_icon(self.path, self.target, provider, icon_px,
                                                   self.is_folder, self.launch_uri)

    def learn(self, name: str) -> None:
        self.learned.add(name)
        self.match_names.add(name)

    def bounce_offset(self, now: float, size: float) -> float:
        if self.bounce_t0 is None:
            return 0.0
        t = now - self.bounce_t0
        if t >= BOUNCE_TIME:
            self.bounce_t0 = None
            return 0.0
        return size * 0.42 * abs(math.sin(math.pi * t / 0.5)) * (1 - 0.35 * t)

    def to_json(self) -> dict:
        data = {"name": self.name, "path": self.path}
        if self.learned:
            data["match"] = sorted(self.learned)
        if self.launch_uri:
            data["launch_uri"] = self.launch_uri
        return data


class Separator:
    transient = False
    fixed = False
    is_sep = True
    is_trash = False
    name = ""
    running = False
    bounce_t0 = None

    def bounce_offset(self, _now: float, _size: float) -> float:
        return 0.0

    def to_json(self) -> dict:
        return {"type": "separator"}


class TrashItem:
    """A Lixeira do Windows: ícone vazio/cheio, abrir, receber arquivos, esvaziar."""
    is_sep = False
    is_trash = True
    transient = False
    fixed = False
    is_folder = False
    is_web = False
    name = "Lixeira"
    running = False
    bounce_t0 = None
    learn_t0 = None
    icon_missing: list[str] = []
    match_names: set[str] = set()

    def __init__(self, icon_px: int) -> None:
        self.full = recycle_bin_count() > 0
        self.reload_icon(None, icon_px)

    def reload_icon(self, _provider, icon_px: int) -> None:
        self.icons = {}
        for full, siid in ((False, SIID_RECYCLER), (True, SIID_RECYCLERFULL)):
            file, index = stock_icon_location(siid)
            pm = extract_icon(file, index) if file else QPixmap()
            if pm.width() > icon_px:
                pm = pm.scaled(icon_px, icon_px, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            self.icons[full] = pm

    @property
    def pixmap(self) -> QPixmap:
        return self.icons[self.full]

    def open_with_command(self) -> None:
        return None

    def bounce_offset(self, _now: float, _size: float) -> float:
        return 0.0

    def to_json(self) -> dict:
        return {"type": "trash"}


class SystemItem:
    fixed = True
    transient = False
    is_sep = False
    is_trash = False
    is_folder = False
    running = False
    bounce_t0 = None
    learn_t0 = None
    icon_missing = ()
    match_names = frozenset()

    def __init__(self, name, icon_px, *, accessibility=False):
        self.name = name
        self.accessibility = accessibility
        self.reload_icon(None, icon_px)

    def reload_icon(self, _provider, icon_px):
        self.icon = QPixmap(icon_px, icon_px)
        self.icon.fill(Qt.transparent)
        painter = QPainter(self.icon)
        painter.setRenderHint(QPainter.Antialiasing)
        inset = icon_px * 0.10
        tile = QRectF(inset, inset, icon_px - 2 * inset, icon_px - 2 * inset)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#2263b4") if not self.accessibility else QColor("#2462c4"))
        painter.drawRoundedRect(tile, inset * 1.7, inset * 1.7)
        painter.setBrush(Qt.white)
        if not self.accessibility:
            unit = icon_px * 0.18
            gap = icon_px * 0.035
            start = (icon_px - 2 * unit - gap) / 2
            for x in (start, start + unit + gap):
                for y in (start, start + unit + gap):
                    painter.drawRect(QRectF(x, y, unit, unit))
        else:
            painter.scale(icon_px / 100, icon_px / 100)
            pen = QPen(Qt.white, 4.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(50, 50), 30, 30)
            painter.setPen(Qt.NoPen)
            painter.setBrush(Qt.white)
            painter.drawEllipse(QPointF(50, 34.5), 4.5, 4.5)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            arms = QPainterPath(QPointF(34.5, 44))
            arms.quadTo(50, 51.5, 65.5, 44)
            painter.drawPath(arms)
            painter.drawLine(QPointF(50, 48), QPointF(50, 57))
            legs = QPainterPath(QPointF(42.5, 70))
            legs.lineTo(50, 57)
            legs.lineTo(57.5, 70)
            painter.drawPath(legs)
        painter.end()

    @property
    def pixmap(self):
        return self.icon

    def bounce_offset(self, _now, _size):
        return 0.0

    def open_with_command(self):
        return None


def make_item(data: dict, provider: QFileIconProvider, icon_px: int):
    if data.get("type") == "separator":
        return Separator()
    if data.get("type") == "trash":
        return TrashItem(icon_px)
    return DockItem(data["name"], data["path"], provider, icon_px,
                    data.get("match"), data.get("launch_uri", ""))


# --------------------------------------------------------------------------
# O dock
# --------------------------------------------------------------------------
MENU_STYLE = """
QMenu { background: #2a2a2e; color: #f2f2f2; border: 1px solid #45454c; padding: 5px; }
QMenu::item { padding: 6px 26px 6px 24px; border-radius: 5px; }
QMenu::item:selected { background: #3b6fe0; }
QMenu::item:disabled { color: #9a9aa2; }
QMenu::separator { height: 1px; background: #45454c; margin: 5px 8px; }
"""
MENU_STYLE_LIGHT = """
QMenu { background: #f6f6f8; color: #1d1d1f; border: 1px solid #c9c9cf; padding: 5px; }
QMenu::item { padding: 6px 26px 6px 24px; border-radius: 5px; }
QMenu::item:selected { background: #3b6fe0; color: #ffffff; }
QMenu::item:disabled { color: #8a8a90; }
QMenu::separator { height: 1px; background: #d4d4d9; margin: 5px 8px; }
"""

THEMES = {
    "dark": {
        "panel": QColor(24, 24, 28, 150), "panel_blur": QColor(24, 24, 28, 110),
        "grad_top": QColor(255, 255, 255, 46), "grad_bottom": QColor(255, 255, 255, 12),
        "border": QColor(255, 255, 255, 70), "dot": QColor(255, 255, 255, 210),
        "sep": QColor(255, 255, 255, 85), "label_bg": QColor(32, 32, 36, 235),
        "label_border": QColor(255, 255, 255, 55), "label_text": QColor(245, 245, 245),
        "highlight": QColor(255, 255, 255, 60), "menu": MENU_STYLE,
    },
    "light": {
        "panel": QColor(246, 246, 248, 170), "panel_blur": QColor(246, 246, 248, 125),
        "grad_top": QColor(255, 255, 255, 90), "grad_bottom": QColor(255, 255, 255, 25),
        "border": QColor(0, 0, 0, 45), "dot": QColor(25, 25, 25, 200),
        "sep": QColor(0, 0, 0, 60), "label_bg": QColor(250, 250, 250, 242),
        "label_border": QColor(0, 0, 0, 45), "label_text": QColor(25, 25, 25),
        "highlight": QColor(0, 0, 0, 38), "menu": MENU_STYLE_LIGHT,
    },
}
PANEL_RADIUS = 18
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10


class BlurBehind(QWidget):
    """Janela vazia logo atrás do painel, do tamanho exato dele: o Windows desfoca
    o que está por trás dela, e o painel do dock é desenhado por cima. (Aplicar o
    desfoque na janela do dock desfocaria um retângulo bem maior que o painel.)"""

    def __init__(self, radius=PANEL_RADIUS) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.blur_on = False
        self.radius = radius
        self.region_key = None

    def place(self, rect: QRect, dock_hwnd: int) -> None:
        if rect.width() < 8 or rect.height() < 8:
            if self.isVisible():
                self.hide()
            return
        if rect != self.geometry():
            self.setGeometry(rect)
        newly_shown = not self.isVisible()
        hwnd = int(self.winId())
        dpr = self.devicePixelRatioF()
        client = wintypes.RECT()
        if self.radius and user32.GetClientRect(hwnd, ctypes.byref(client)):
            key = (hwnd, client.right, client.bottom, dpr)
            if key != self.region_key:
                diameter = round(2 * self.radius * dpr)
                region = gdi32.CreateRoundRectRgn(0, 0, client.right, client.bottom, diameter, diameter)
                if region:
                    if user32.SetWindowRgn(hwnd, region, True):
                        self.region_key = key
                    else:
                        gdi32.DeleteObject(region)
        if newly_shown:
            self.show()
        if not self.blur_on:
            self.blur_on = enable_blur(hwnd)
        if newly_shown:
            self.stay_below(dock_hwnd)

    def stay_below(self, dock_hwnd: int) -> None:
        if self.isVisible():
            user32.SetWindowPos(int(self.winId()), dock_hwnd, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


class DisplayDevice(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("name", wintypes.WCHAR * 32),
        ("description", wintypes.WCHAR * 128), ("state", wintypes.DWORD),
        ("device_id", wintypes.WCHAR * 128), ("registry_key", wintypes.WCHAR * 128),
    ]


_proto(user32.EnumDisplayDevicesW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD,
       ctypes.POINTER(DisplayDevice), wintypes.DWORD)


def screen_preference_key(screen):
    if serial := screen.serialNumber().strip():
        return "|".join((screen.manufacturer(), screen.model(), serial)).casefold()
    display = screen.name()
    if re.fullmatch(r"DISPLAY\d+", display, re.IGNORECASE):
        display = '\\\\.\\' + display
    device = DisplayDevice(size=ctypes.sizeof(DisplayDevice))
    if user32.EnumDisplayDevicesW(display, 0, ctypes.byref(device), 1) and device.device_id:
        return device.device_id.casefold()
    return screen.name().casefold()


class DockGroup:
    """O que é comum a todos os docks (um por monitor, se "Mostrar em todas as
    telas" estiver ligado): configuração, itens e ícones. Os docks compartilham
    a mesma lista de itens, então o que muda num aparece nos outros.
    O dock da tela principal é o "líder": registra o atalho de teclado, procura
    atualizações e acompanha apps abertos e a Lixeira para todos."""

    def __init__(self) -> None:
        self.cfg = load_config()
        self.provider = QFileIconProvider()
        self.icon_px = self.needed_icon_px()
        self.items = [make_item(a, self.provider, self.icon_px) for a in self.cfg["apps"]]
        if not self.cfg.get("trash_added"):
            # Primeira vez com a Lixeira: entra no fim, depois de um separador.
            if not any(it.is_trash for it in self.items):
                if self.items and not self.items[-1].is_sep:
                    self.items.append(Separator())
                self.items.append(TrashItem(self.icon_px))
            self.cfg["trash_added"] = True
            self.save()
        self.docks: list[Dock] = []
        self.top_bar = None
        self.welcome = None
        self._rebuild_timer: QTimer | None = None
        self.window_paths = {}
        self.app_window_handles = {}
        if DESKTOP_MODE:
            trash = next((item for item in self.items if item.is_trash), None) or TrashItem(self.icon_px)
            trash.fixed = True
            explorers = [item for item in self.items if self.is_explorer(item)]
            explorer = explorers[0] if explorers else DockItem(
                "Explorador de Arquivos", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe"),
                self.provider, self.icon_px,
            )
            explorer.fixed = True
            self.items = [item for item in self.items if not item.is_trash and item not in explorers]
            while self.items and self.items[0].is_sep:
                self.items.pop(0)
            while self.items and self.items[-1].is_sep:
                self.items.pop()
            self.start_item = SystemItem("Iniciar", self.icon_px)
            self.access_item = SystemItem("Acessibilidade", self.icon_px, accessibility=True)
            start_separator = Separator()
            self.end_separator = Separator()
            start_separator.fixed = self.end_separator.fixed = True
            self.items[:0] = [self.start_item, start_separator]
            self.items.extend((self.end_separator, explorer, self.access_item, trash))

    @staticmethod
    def is_explorer(item) -> bool:
        return isinstance(item, DockItem) and os.path.basename(item.target).casefold() == "explorer.exe"

    @property
    def leader(self) -> Dock | None:
        return self.docks[0] if self.docks else None

    def needed_icon_px(self) -> int:
        """Maior tamanho (em pixels reais) em que um ícone é desenhado: ampliado ou
        sendo arrastado, na tela de maior escala (125%, 150%...)."""
        dpr = max((s.devicePixelRatio() for s in QGuiApplication.screens()), default=1.0)
        biggest = float(self.cfg["icon_size"]) * max(float(self.cfg["magnification"]), 1.15)
        return math.ceil(biggest * dpr)

    def reload_icons(self) -> None:
        px = self.needed_icon_px()
        if px == self.icon_px:
            return
        self.icon_px = px
        for it in self.items:
            if not it.is_sep:
                it.reload_icon(self.provider, px)
        QTimer.singleShot(1000, trim_memory)

    def save(self) -> None:
        self.cfg["apps"] = [
            it.to_json() for it in self.items
            if not it.transient and (not it.fixed or it.is_trash)
        ]
        save_config(self.cfg)

    def show_welcome(self):
        if self.welcome is None:
            self.welcome = WelcomeDialog(self)
            self.welcome.finished.connect(self.finish_welcome)
        self.welcome.show()
        self.welcome.raise_()
        self.welcome.activateWindow()

    def finish_welcome(self, _result):
        self.welcome = None
        if not self.cfg["welcome_seen"]:
            self.cfg["welcome_seen"] = True
            self.save()

    # ---- um dock por tela ------------------------------------------------
    def dock_on_screen(self, screen):
        choices = self.cfg.get("dock_monitors") or {}
        default = self.cfg.get("all_screens") or screen is QGuiApplication.primaryScreen()
        return choices.get(screen_preference_key(screen), bool(default))

    def target_screens(self) -> list:
        primary = QGuiApplication.primaryScreen()
        screens = [primary] + [s for s in QGuiApplication.screens() if s is not primary]
        return [s for s in screens if s and self.dock_on_screen(s)] or ([primary] if primary else [])

    def top_bar_screen(self):
        key = self.cfg.get("top_bar_monitor")
        return next((s for s in QGuiApplication.screens() if key and screen_preference_key(s) == key),
                    QGuiApplication.primaryScreen())

    def set_dock_monitor(self, key, enabled):
        self.cfg.setdefault("dock_monitors", {})[key] = enabled
        self.save()
        self.rebuild()

    def set_top_bar_monitor(self, key):
        self.cfg["top_bar_monitor"] = key
        self.save()
        if self.top_bar is not None:
            self.top_bar.follow_screen(self.top_bar_screen())

    def focus_dock(self):
        screen = QGuiApplication.screenAt(QCursor.pos())
        dock = next((dock for dock in self.docks if dock.screen_ref is screen), self.leader)
        if dock is not None:
            dock.enter_keyboard_mode()

    def build(self) -> None:
        for screen in self.target_screens():
            dock = Dock(self, screen)
            self.docks.append(dock)
            dock.show()

    def close_docks(self) -> None:
        docks, self.docks = self.docks, []
        for dock in docks:
            dock.shutdown()

    def rebuild(self) -> None:
        """Monitor ligado/desligado, tela principal trocada ou opção mudada: recria
        os docks (com um pequeno atraso, porque o Windows avisa várias vezes)."""
        if self._rebuild_timer is None:
            self._rebuild_timer = QTimer(singleShot=True, interval=700, timeout=self._do_rebuild)
        self._rebuild_timer.start()

    def _do_rebuild(self) -> None:
        self.close_docks()
        self.build()
        if self.top_bar is not None:
            self.top_bar.follow_screen(self.top_bar_screen())
        log(f"docks recriados: {len(self.docks)} tela(s)")

    def watch_screens(self) -> None:
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda _s: self.rebuild())
        app.screenRemoved.connect(lambda _s: self.rebuild())
        app.primaryScreenChanged.connect(lambda _s: self.rebuild())

    # ---- avisar os outros docks ------------------------------------------
    def refresh(self, source: Dock | None = None) -> None:
        """Itens mudaram (adicionar, remover, reordenar...): redesenha os outros docks."""
        for dock in self.docks:
            if dock is not source:
                dock.reposition()
                dock.last_state = None

    def update_all(self) -> None:
        for dock in self.docks:
            dock.update()

    def set_forced(self, value: str | None) -> None:
        for dock in self.docks:
            dock.forced = value
            if value == "show" and not dock.isVisible():
                dock.reveal = 0.0
                dock.show()


class ThumbnailProperties(ctypes.Structure):
    _fields_ = [
        ("flags", wintypes.DWORD), ("destination", wintypes.RECT), ("source", wintypes.RECT),
        ("opacity", wintypes.BYTE), ("visible", wintypes.BOOL), ("client_only", wintypes.BOOL),
    ]


_proto(dwmapi.DwmRegisterThumbnail, ctypes.c_long, HWND, HWND, ctypes.POINTER(wintypes.HANDLE))
_proto(dwmapi.DwmQueryThumbnailSourceSize, ctypes.c_long, wintypes.HANDLE, ctypes.POINTER(wintypes.SIZE))
_proto(dwmapi.DwmUpdateThumbnailProperties, ctypes.c_long, wintypes.HANDLE,
       ctypes.POINTER(ThumbnailProperties))
_proto(dwmapi.DwmUnregisterThumbnail, ctypes.c_long, wintypes.HANDLE)


def register_thumbnail(destination, hwnd):
    handle = wintypes.HANDLE()
    if dwmapi.DwmRegisterThumbnail(int(destination.winId()), hwnd, ctypes.byref(handle)) != 0:
        return None
    size = wintypes.SIZE()
    if dwmapi.DwmQueryThumbnailSourceSize(handle, ctypes.byref(size)) == 0 and size.cx > 0 and size.cy > 0:
        return handle, QSize(size.cx, size.cy)
    dwmapi.DwmUnregisterThumbnail(handle)
    return None


def place_thumbnail(handle, destination, rect):
    scale = destination.devicePixelRatio()
    native_rect = wintypes.RECT(
        round(rect.x() * scale), round(rect.y() * scale),
        round((rect.x() + rect.width()) * scale), round((rect.y() + rect.height()) * scale),
    )
    props = ThumbnailProperties(0x01 | 0x04 | 0x08, native_rect, wintypes.RECT(), 255, True, False)
    return dwmapi.DwmUpdateThumbnailProperties(handle, ctypes.byref(props)) == 0


class WindowPeek(QWidget):
    def __init__(self, previews):
        super().__init__(previews, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setObjectName("windowPeek")
        self.setStyleSheet("QWidget#windowPeek { background: #25282e; border: 1px solid #555962; }")
        self.setWindowTitle("Prévia ampliada — Pingo Dock")
        self.setScreen(previews.dock.screen_ref)
        self.thumbnail = None

    def show_window(self, hwnd, screen, anchor_top):
        self.clear()
        margin = 6
        top_limit = screen.top() + 42
        bottom = anchor_top - 10
        available = QSize(round(screen.width() * 0.84), bottom - top_limit - 2 * margin)
        if available.height() < 120:
            return
        result = register_thumbnail(self, hwnd)
        if result is None:
            return

        handle, source = result
        image = source.scaled(available, Qt.KeepAspectRatio)
        frame = image + QSize(2 * margin, 2 * margin)
        self.setGeometry(screen.x() + (screen.width() - frame.width()) // 2,
                         bottom - frame.height(), frame.width(), frame.height())
        if not place_thumbnail(handle, self, QRect(QPoint(margin, margin), image)):
            dwmapi.DwmUnregisterThumbnail(handle)
            return
        self.thumbnail = handle
        self.show()

    def clear(self):
        self.hide()
        if self.thumbnail is not None:
            dwmapi.DwmUnregisterThumbnail(self.thumbnail)
            self.thumbnail = None


MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
_proto(user32.EnumDisplayMonitors, wintypes.BOOL, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
       MONITORENUMPROC, wintypes.LPARAM)


def monitor_info(monitor):
    info = MONITORINFOEX()
    info.cbSize = ctypes.sizeof(info)
    return info if user32.GetMonitorInfoW(monitor, ctypes.byref(info)) else None


def monitor_label(info):
    for index, screen in enumerate(QGuiApplication.screens(), 1):
        if screen.name().casefold() == info.device.casefold():
            return f"Monitor {index}"
    number = re.search(r"DISPLAY(\d+)$", info.device, re.IGNORECASE)
    return f"Monitor {number[1]}" if number else info.device


def window_monitor_label(hwnd):
    info = monitor_info(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))
    return monitor_label(info) if info else "Monitor"


def other_window_monitors(hwnd):
    current = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    monitors = []

    @MONITORENUMPROC
    def collect(monitor, _dc, _rect, _data):
        if monitor != current and (info := monitor_info(monitor)):
            monitors.append((monitor_label(info), monitor))
        return True

    user32.EnumDisplayMonitors(None, None, collect, 0)
    return sorted(monitors)


def arrange_window(hwnd, side=None, destination=None):
    source = monitor_info(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))
    target = monitor_info(destination) if destination is not None else source
    if source is None or target is None:
        return False

    maximized = bool(user32.IsZoomed(hwnd))
    if maximized or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    outer, frame = wintypes.RECT(), wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(outer)):
        return False
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame)) != 0:
        frame = outer

    work = target.rcWork
    if side:
        middle = work.left + (work.right - work.left) // 2
        x, right = (work.left, middle) if side == "left" else (middle, work.right)
        y, width, height = work.top, right - x, work.bottom - work.top
    else:
        width = min(frame.right - frame.left, work.right - work.left)
        height = min(frame.bottom - frame.top, work.bottom - work.top)
        x = max(work.left, min(work.right - width, work.left + frame.left - source.rcWork.left))
        y = max(work.top, min(work.bottom - height, work.top + frame.top - source.rcWork.top))

    moved = user32.SetWindowPos(hwnd, None, x + outer.left - frame.left,
                               y + outer.top - frame.top,
                               width + (outer.right - frame.right) + (frame.left - outer.left),
                               height + (outer.bottom - frame.bottom) + (frame.top - outer.top),
                               SWP_NOACTIVATE | 0x0004)  # SWP_NOZORDER
    if maximized and (not side or not moved):
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
    return bool(moved)


class WindowPreviews(QWidget):
    def __init__(self, dock, item, windows, anchor):
        super().__init__(dock, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setObjectName("windowPreviews")
        self.setWindowTitle("Janelas — " + item.name)
        self.setStyleSheet("""
            QWidget#windowPreviews { background: #25282e; border: 1px solid #555962; border-radius: 9px; }
            QToolButton { color: #f4f5f7; background: #343841; border: 0; border-radius: 5px; }
            QToolButton:hover { background: #4b5361; }
            QLabel { color: #f4f5f7; font-size: 9pt; }
        """)
        self.dock, self.item, self.windows, self.anchor = dock, item, windows, anchor
        self.thumbnails = []
        self.page = 0
        self.page_size = max(1, min(4, (dock.screen_ref.geometry().width() - 18) // 172))
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(9, 9, 9, 9)
        self.body = None
        self.peek = None
        self.peek_target = None
        self.peek_since = None
        self.preview_cards = []
        self.options_open = False
        self.close_buttons = []
        self.window_badges = {}
        self.active_window = None
        self.next_monitor_check = 0.0
        self.render_page()

    def clear_thumbnails(self):
        self.preview_cards.clear()
        self.close_buttons.clear()
        self.window_badges.clear()
        self.active_window = None
        self.next_monitor_check = 0.0
        self.peek_target = None
        self.peek_since = None
        self.release_peek()
        for thumbnail in self.thumbnails:
            dwmapi.DwmUnregisterThumbnail(thumbnail)
        self.thumbnails.clear()

    def render_page(self):
        self.clear_thumbnails()
        if self.body is not None:
            self.root.removeWidget(self.body)
            self.body.hide()
            self.body.deleteLater()
        self.body = QWidget()
        self.root.addWidget(self.body)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        cards = QHBoxLayout()
        body_layout.addLayout(cards)
        targets = []
        start = self.page * self.page_size
        for hwnd in self.windows[start:start + self.page_size]:
            title = window_title(hwnd) or self.item.name
            button = QToolButton()
            button.setFixedSize(162, 120)
            button.setToolTip(title)
            button.setAccessibleName(title)
            self.preview_cards.append((button, hwnd))
            card = QVBoxLayout(button)
            card.setContentsMargins(6, 6, 6, 5)
            card.setSpacing(2)
            close = QToolButton(button)
            close.setText("×")
            close.setFixedSize(23, 23)
            close.setAccessibleName(f"Fechar janela: {title}")
            close.setToolTip("Fechar janela")
            close.setStyleSheet("QToolButton { background: #25282e; font-size: 12pt; }"
                                "QToolButton:hover { background: #ab3645; }")
            close.clicked.connect(lambda _checked=False, window=hwnd: self.request_close(window))
            header = QHBoxLayout()
            badge = QLabel()
            badge.setAttribute(Qt.WA_TransparentForMouseEvents)
            header.addWidget(badge)
            header.addStretch()
            header.addWidget(close)
            card.addLayout(header)
            self.close_buttons.append(close)
            self.window_badges[hwnd] = (badge, window_monitor_label(hwnd))
            image = QLabel()
            image.setAttribute(Qt.WA_TransparentForMouseEvents)
            image.setAlignment(Qt.AlignCenter)
            image.setPixmap(self.item.pixmap.scaled(38, 38, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            card.addWidget(image, 1)
            caption = QLabel()
            caption.setAttribute(Qt.WA_TransparentForMouseEvents)
            caption.setTextFormat(Qt.PlainText)
            caption.setText(caption.fontMetrics().elidedText(title, Qt.ElideRight, 150))
            card.addWidget(caption)
            cards.addWidget(button)
            button.clicked.connect(lambda _checked=False, window=hwnd: self.select(window))
            button.setContextMenuPolicy(Qt.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda point, window=hwnd, card=button: self.window_options(window, card.mapToGlobal(point)))
            targets.append((hwnd, image))
        pages = math.ceil(len(self.windows) / self.page_size)
        if pages > 1:
            navigation = QHBoxLayout()
            body_layout.addLayout(navigation)
            for arrow, direction in (("‹", -1), ("›", 1)):
                button = QToolButton()
                button.setText(arrow)
                button.setAccessibleName("Página anterior" if direction < 0 else "Próxima página")
                button.setEnabled(0 <= self.page + direction < pages)
                button.clicked.connect(lambda _checked=False, step=direction: self.change_page(step))
                navigation.addWidget(button)
                if direction < 0:
                    navigation.addWidget(QLabel(f"{self.page + 1} / {pages}"), 1, Qt.AlignCenter)
        self.adjustSize()
        area = self.dock.screen_ref.geometry()
        x = min(max(area.left(), self.anchor.x() - self.width() // 2),
                area.right() - self.width() + 1)
        self.move(x, max(area.top(), self.anchor.y() - self.height() - 7))
        self.show()
        self.update_active_badges()
        for hwnd, image in targets:
            self.attach_thumbnail(hwnd, image)

    def attach_thumbnail(self, hwnd, image):
        result = register_thumbnail(self, hwnd)
        if result is None:
            return
        thumbnail, source = result
        size = source.scaled(image.size(), Qt.KeepAspectRatio)
        inset = QPoint((image.width() - size.width()) // 2, (image.height() - size.height()) // 2)
        if place_thumbnail(thumbnail, self, QRect(image.mapTo(self, inset), size)):
            self.thumbnails.append(thumbnail)
            image.clear()
            return
        dwmapi.DwmUnregisterThumbnail(thumbnail)

    def track_peek(self, global_pos, now):
        self.update_active_badges()
        hwnd = next((window for button, window in self.preview_cards
                     if button.rect().contains(button.mapFromGlobal(global_pos))), None)
        if any(button.rect().contains(button.mapFromGlobal(global_pos)) for button in self.close_buttons):
            hwnd = None
        if hwnd != self.peek_target:
            self.release_peek()
            self.peek_target = hwnd
            self.peek_since = now if hwnd is not None else None
        elif self.peek_since is not None and now - self.peek_since >= 0.32:
            self.peek_since = None
            self.show_peek(hwnd)

    def update_active_badges(self):
        active = user32.GetForegroundWindow() or 0
        now = time.monotonic()
        refresh_monitors = now >= self.next_monitor_check
        if active == self.active_window and not refresh_monitors:
            return
        if refresh_monitors:
            self.next_monitor_check = now + 0.5
        self.active_window = active
        for hwnd, (badge, monitor) in self.window_badges.items():
            if refresh_monitors:
                monitor = window_monitor_label(hwnd)
                self.window_badges[hwnd] = (badge, monitor)
            text = ("● " if hwnd == active else "") + monitor
            if badge.text() != text:
                badge.setText(text)
                badge.setStyleSheet("color: #a6d7ff;" if hwnd == active else "color: #c6cbd4;")
                badge.setToolTip("Janela ativa" if hwnd == active else monitor)

    def show_peek(self, hwnd):
        self.release_peek()
        self.peek = WindowPeek(self)
        self.peek.show_window(hwnd, self.dock.screen_ref.geometry(), self.geometry().top())
        if not self.peek.isVisible():
            self.release_peek()

    def release_peek(self):
        if self.peek is not None:
            self.peek.clear()
            self.peek.deleteLater()
            self.peek = None

    def hideEvent(self, event):
        self.clear_thumbnails()
        super().hideEvent(event)

    def select(self, hwnd):
        self.dock.close_previews()
        bring_to_front(hwnd)

    def window_options(self, hwnd, point):
        self.release_peek()
        self.peek_target = self.peek_since = None
        menu = QMenu(self)
        menu.setFont(self.font())
        menu.setStyleSheet(self.dock.pal["menu"])
        left = menu.addAction("Encaixar à esquerda")
        right = menu.addAction("Encaixar à direita")
        destinations = {}
        monitors = other_window_monitors(hwnd)
        if monitors:
            move = menu.addMenu("Mover para outro monitor")
            for label, monitor in monitors:
                destinations[move.addAction(label)] = monitor

        self.options_open = True
        try:
            action = menu.exec(point)
        finally:
            self.options_open = False
            menu.deleteLater()
            self.dock.preview_since = time.monotonic()

        if action in (left, right) or action in destinations:
            side = "left" if action == left else "right" if action == right else None
            if arrange_window(hwnd, side, destinations.get(action)):
                self.select(hwnd)

    def request_close(self, hwnd):
        self.release_peek()
        self.peek_target = self.peek_since = None
        close_window(hwnd)

    def refresh_windows(self, windows):
        self.windows = windows
        self.page = min(self.page, (len(windows) - 1) // self.page_size)
        self.render_page()

    def change_page(self, step):
        self.page += step
        self.render_page()


class ShellFileInfo(ctypes.Structure):
    _fields_ = [
        ("icon", wintypes.HICON), ("icon_index", ctypes.c_int), ("attributes", wintypes.DWORD),
        ("display_name", wintypes.WCHAR * 260), ("type_name", wintypes.WCHAR * 80),
    ]


ole32 = ctypes.WinDLL("ole32")
_proto(ole32.CoInitializeEx, ctypes.c_long, ctypes.c_void_p, wintypes.DWORD)
_proto(ole32.CoUninitialize, None)
_proto(ole32.CoTaskMemFree, None, ctypes.c_void_p)
_proto(shell32.SHParseDisplayName, ctypes.c_long, wintypes.LPCWSTR, ctypes.c_void_p,
       ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD, ctypes.c_void_p)
_proto(shell32.SHGetFileInfoW, ctypes.c_size_t, ctypes.c_void_p, wintypes.DWORD,
       ctypes.POINTER(ShellFileInfo), wintypes.UINT, wintypes.UINT)

advapi32 = ctypes.WinDLL("advapi32")
_proto(advapi32.OpenProcessToken, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
       ctypes.POINTER(wintypes.HANDLE))
_proto(advapi32.GetTokenInformation, wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
_proto(advapi32.ConvertSidToStringSidW, wintypes.BOOL, ctypes.c_void_p,
       ctypes.POINTER(wintypes.LPWSTR))
_proto(kernel32.LocalFree, ctypes.c_void_p, ctypes.c_void_p)


def current_user_sid():
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None  # TOKEN_QUERY
    try:
        length = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))  # TokenUser
        if not length.value:
            return None
        user = ctypes.create_string_buffer(length.value)
        if not advapi32.GetTokenInformation(token, 1, user, length, ctypes.byref(length)):
            return None
        sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        value = wintypes.LPWSTR()
        if advapi32.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            try:
                return value.value
            finally:
                kernel32.LocalFree(ctypes.cast(value, ctypes.c_void_p))
        return None
    finally:
        kernel32.CloseHandle(token)


class StartLauncher(QWidget):
    MAX_CACHED_ICONS = 48

    def __init__(self, dock):
        super().__init__(dock, Qt.Popup | Qt.FramelessWindowHint)
        self.dock = dock
        self.setWindowTitle("Iniciar — Pingo Dock")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("startLauncher")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar aplicativos")
        self.search.setAccessibleName("Buscar aplicativos")
        self.apps = QTreeWidget()
        self.apps.setHeaderHidden(True)
        self.apps.setRootIsDecorated(False)
        self.apps.setExpandsOnDoubleClick(False)
        self.apps.setUniformRowHeights(True)
        self.apps.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.apps.setIconSize(QSize(22, 22))
        self.apps.setIndentation(19)
        self.apps.setAccessibleName("Aplicativos do menu Iniciar")
        self.status = QLabel()
        self.account_name = os.environ.get("USERNAME") or "Conta"
        self.account_photo = QPixmap()
        self.account = QToolButton()
        self.account.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.account.setFixedSize(161, 32)
        self.account.setIconSize(QSize(24, 24))
        self.account.clicked.connect(lambda: self.open_location("ms-settings:yourinfo"))
        footer = QHBoxLayout()
        footer.setSpacing(3)
        footer.addWidget(self.account)
        footer.addStretch()
        self.footer_icons = []
        self.add_footer_button(footer, "Configurações", "\ue713", lambda: self.open_location("ms-settings:"))
        self.power_button = self.add_footer_button(footer, "Energia", "\ue7e8", self.open_power_menu)
        if not SHELL_PROTOTYPE:
            self.add_footer_button(footer, "Menu do Windows", "\ue782", self.open_windows_start)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 13, 13, 11)
        layout.addWidget(self.search)
        layout.addWidget(self.apps, 1)
        layout.addWidget(self.status)
        layout.addLayout(footer)
        self.search.textChanged.connect(self.filter_apps)
        self.search.returnPressed.connect(self.select_search_result)
        self.apps.itemActivated.connect(self.activate_entry)
        self.apps.itemClicked.connect(self.activate_entry)
        self.apps.itemExpanded.connect(self.decorate_visible)
        self.apps.itemExpanded.connect(lambda item: self.remember_folder(item, True))
        self.apps.itemCollapsed.connect(lambda item: self.remember_folder(item, False))
        self.apps.verticalScrollBar().valueChanged.connect(self.on_scroll)
        self.search.installEventFilter(self)
        self.apps.installEventFilter(self)
        self.entries = []
        self.shortcut_entries = []
        self.catalog_records = []
        self.icons = OrderedDict()
        self.decorated_items = {}
        self.icon_batch = None
        self.shortcut_result = None
        self.expanded_folders = set()
        self.catalog_loaded_at = 0.0
        self.icon_refresh = QTimer(self, singleShot=True, interval=30, timeout=self.load_visible_icons)
        self.shortcut_poll = QTimer(self, interval=40, timeout=self.read_shortcuts)
        self.catalog_refresh = QTimer(self, singleShot=True, interval=150, timeout=self.refresh_catalog_view)
        self.folder_icon = self.dock.provider.icon(QFileIconProvider.Folder)
        self.generic_icon = self.dock.provider.icon(QFileIconProvider.File)
        self.catalog_state = "loading"
        self.catalog = QProcess(self)
        self.catalog.finished.connect(self.read_catalog)
        self.catalog.errorOccurred.connect(self.catalog_error)
        self.load_account_photo(current_user_sid())

    def add_footer_button(self, layout, label, glyph, action):
        button = QToolButton()
        button.setAccessibleName(label)
        button.setToolTip(label)
        button.setProperty("footerIcon", True)
        button.setFixedSize(30, 32)
        button.setIconSize(QSize(16, 16))
        button.clicked.connect(action)
        layout.addWidget(button)
        self.footer_icons.append((button, glyph))
        return button

    @staticmethod
    def search_key(text):
        return "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                       if not unicodedata.combining(c))

    @staticmethod
    def start_shortcuts():
        entries = {}
        for variable in ("ProgramData", "APPDATA"):
            base = os.environ.get(variable)
            if not base:
                continue
            root = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            display_folders = {root: ()}
            for directory, subdirs, files in os.walk(root):
                subdirs[:] = [name for name in subdirs if not name.startswith(".")]
                path_to_folder = Path(directory)
                folder = display_folders[path_to_folder]
                for name in subdirs:
                    child = path_to_folder / name
                    info = ShellFileInfo()
                    path_buffer = ctypes.c_wchar_p(str(child))
                    shell32.SHGetFileInfoW(ctypes.cast(path_buffer, ctypes.c_void_p), 0,
                                           ctypes.byref(info), ctypes.sizeof(info), 0x200)
                    display_folders[child] = (*folder, info.display_name or name)
                for file in files:
                    if file.casefold() in ("desktop.ini", "thumbs.db") or file.startswith("."):
                        continue
                    path = Path(directory) / file
                    name = path.stem if path.suffix.lower() in (".lnk", ".url", ".appref-ms") else file
                    key = tuple(part.casefold() for part in (*folder, name))
                    entries[key] = (folder, name, str(path))
        return list(entries.values())

    def apply_theme(self):
        if self.dock.theme_name == "light":
            surface, border, text, muted, control, hover = (
                "#f5f5f8", "#c9cbd3", "#22252c", "#606675", "#e7e9ef", "#d9e3f4",
            )
        else:
            surface, border, text, muted, control, hover = (
                "#272a31", "#4d525e", "#f4f5f7", "#aeb7c7", "#333841", "#424e62",
            )
        self.section_color = QColor(muted)
        self.surface_color, self.border_color = QColor(surface), QColor(border)
        self.footer_color = QColor(text)
        palette = self.apps.palette()
        palette.setColor(QPalette.Text, QColor(text))
        palette.setColor(QPalette.WindowText, QColor(text))
        self.apps.setPalette(palette)
        self.setStyleSheet(f"""
            QLineEdit, QTreeWidget, QLabel, QToolButton {{ color: {text}; font-size: 10pt; }}
            QLineEdit {{ background: {control}; border: 1px solid {border}; border-radius: 8px; padding: 9px 11px; }}
            QTreeWidget {{ background: transparent; border: 0; outline: 0; }}
            QTreeWidget::item {{ height: 34px; padding: 0 7px; border-radius: 6px; }}
            QTreeWidget::item:hover, QTreeWidget::item:selected {{ background: {hover}; color: {text}; }}
            QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px 1px; }}
            QScrollBar::handle:vertical {{ background: {border}; min-height: 20px; border-radius: 3px; }}
            QScrollBar::handle:vertical:hover {{ background: {muted}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QLabel {{ color: {muted}; padding: 6px; }}
            QToolButton {{ background: {control}; border: 0; border-radius: 6px; padding: 6px 9px; }}
            QToolButton:hover {{ background: {hover}; }}
            QToolButton[footerIcon="true"] {{ background: transparent; padding: 0; }}
            QToolButton[footerIcon="true"]:hover {{ background: {hover}; }}
        """)
        self.refresh_footer()

    def footer_icon(self, glyph, size):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setPen(self.footer_color)
        font = QFont("Segoe MDL2 Assets")
        font.setPixelSize(size - 2)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, glyph)
        painter.end()
        return QIcon(pixmap)

    def refresh_footer(self):
        for button, glyph in self.footer_icons:
            button.setIcon(self.footer_icon(glyph, 18))
        self.account.setAccessibleName(self.account_name)
        self.account.setToolTip(self.account_name)
        text_width = self.account.width() - self.account.iconSize().width() - 24
        self.account.setText(QFontMetrics(self.account.font()).elidedText(
            self.account_name, Qt.ElideRight, text_width
        ))
        if self.account_photo.isNull():
            self.account.setIcon(self.footer_icon("\ue77b", 24))
            return
        avatar = QPixmap(48, 48)
        avatar.fill(Qt.transparent)
        cropped = self.account_photo.scaled(48, 48, Qt.KeepAspectRatioByExpanding,
                                            Qt.SmoothTransformation)
        painter = QPainter(avatar)
        painter.setRenderHint(QPainter.Antialiasing)
        clip = QPainterPath()
        clip.addEllipse(QRectF(0, 0, 48, 48))
        painter.setClipPath(clip)
        painter.drawPixmap((48 - cropped.width()) // 2, (48 - cropped.height()) // 2, cropped)
        painter.end()
        self.account.setIcon(QIcon(avatar))

    def load_account_photo(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
            return
        try:
            account_key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\AccountPicture\Users\{sid}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, account_key) as key:
                for size in (96, 192, 48, 448):
                    try:
                        file, _kind = winreg.QueryValueEx(key, f"Image{size}")
                    except OSError:
                        continue
                    if self.account_photo.load(os.path.expandvars(file)):
                        break
        except OSError:
            pass
    def open_at(self, anchor):
        screen = self.dock.screen_ref.geometry()
        self.apply_theme()
        self.resize(min(420, screen.width()), min(494, max(150, anchor.y() - screen.top() - 10)))
        x = min(max(screen.left(), anchor.x() - self.width() // 2), screen.right() - self.width() + 1)
        self.move(x, max(screen.top(), anchor.y() - self.height() - 8))
        self.search.clear()
        self.filter_apps()
        self.show()
        self.search.setFocus()
        self.decorate_visible()
        if self.catalog.state() == QProcess.NotRunning and time.monotonic() - self.catalog_loaded_at > 60:
            if self.shortcut_result is None:
                self.shortcut_result = queue.SimpleQueue()
                threading.Thread(target=self.collect_shortcuts, args=(self.shortcut_result,), daemon=True).start()
                self.shortcut_poll.start()
            self.catalog.start("powershell.exe", [
                "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
                "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; "
                "$name=(Get-LocalUser -SID $sid -ErrorAction SilentlyContinue).FullName; "
                "ConvertTo-Json -InputObject @{Apps=@(Get-StartApps); Name=$name} -Compress",
            ])

    @classmethod
    def collect_shortcuts(cls, result):
        initialized = ole32.CoInitializeEx(None, 2)
        entries = []
        try:
            entries = cls.start_shortcuts()
        finally:
            if initialized >= 0:
                ole32.CoUninitialize()
            result.put(entries)

    def read_shortcuts(self):
        try:
            self.shortcut_entries = self.shortcut_result.get_nowait()
        except queue.Empty:
            return
        self.shortcut_poll.stop()
        self.shortcut_result = None
        self.merge_catalog()

    def read_catalog(self, code, _status):
        try:
            payload = json.loads(bytes(self.catalog.readAllStandardOutput()).decode("utf-8-sig"))
            if code or not isinstance(payload, dict) or not isinstance(payload.get("Apps"), list):
                raise ValueError("Catálogo do Iniciar indisponível")
            if isinstance(payload.get("Name"), str) and payload["Name"].strip():
                self.account_name = payload["Name"].strip()
                self.refresh_footer()
            self.catalog_records = payload["Apps"]
            self.catalog_state = "ready"
            self.catalog_loaded_at = time.monotonic()
        except (ValueError, TypeError):
            self.catalog_error()
            return
        self.merge_catalog()

    def catalog_error(self, _error=None):
        self.catalog_state = "error"
        self.merge_catalog()

    def merge_catalog(self):
        entries = self.shortcut_entries.copy()
        names = {self.search_key(name) for _, name, _ in entries}
        for record in self.catalog_records:
            if not isinstance(record, dict) or not record.get("Name") or not record.get("AppID"):
                continue
            name = record["Name"]
            key = self.search_key(name)
            if key not in names:
                entries.append(((), name, "shell:AppsFolder\\" + record["AppID"]))
                names.add(key)
        if entries != self.entries or not self.apps.topLevelItemCount():
            self.entries = entries
            self.catalog_refresh.start()

    def on_scroll(self):
        if self.catalog_refresh.isActive():
            self.catalog_refresh.start()
        self.decorate_visible()

    def refresh_catalog_view(self):
        if self.isVisible():
            self.filter_apps(preserve_scroll=True)

    def filter_apps(self, _text=None, *, preserve_scroll=False):
        self.catalog_refresh.stop()
        query = " ".join(self.search_key(self.search.text()).split())
        scroll = self.apps.verticalScrollBar()
        previous = scroll.value() if preserve_scroll else 0
        selected = self.apps.currentItem()
        selected_target = selected.data(0, Qt.UserRole) if query and preserve_scroll and selected else None
        self.apps.setUpdatesEnabled(False)
        self.decorated_items.clear()
        self.apps.clear()
        if query:
            matches = []
            for folder, name, target in self.entries:
                rank = self.search_rank(query, self.search_key(name), self.search_key(" ".join(folder)))
                if rank is not None:
                    matches.append((rank, name, folder, target))
            for _, name, folder, target in sorted(matches, key=lambda entry: (entry[0], self.search_key(entry[1]))):
                item = self.add_app(self.apps, name, target, folder)
                if selected_target == target:
                    self.apps.setCurrentItem(item)
        else:
            tree = {}
            for folder, name, target in self.entries:
                branch = tree
                for part in folder:
                    branch = branch.setdefault((part, ""), {})
                branch[(name, target)] = None
            self.add_tree(self.apps, tree, alphabetical=True)
        self.apps.setUpdatesEnabled(True)
        self.apps.doItemsLayout()
        scroll.setValue(previous)
        self.status.setVisible(self.apps.topLevelItemCount() == 0)
        self.status.setText({
            "loading": "Carregando aplicativos…",
            "error": "Catálogo de aplicativos indisponível.",
            "ready": "Nenhum aplicativo encontrado.",
        }[self.catalog_state])
        self.decorate_visible()

    @staticmethod
    def search_rank(query, name, folder):
        if name == query:
            return 0
        if name.startswith(query):
            return 1
        terms = query.split()
        words = re.findall(r"\w+", name)
        if all(any(word.startswith(term) for word in words) for term in terms):
            return 2
        if all(term in name for term in terms):
            return 3
        if all(term in f"{folder} {name}" for term in terms):
            return 4
        return None

    def add_tree(self, parent, branch, alphabetical=False, path=()):
        section, letter = parent, None
        def sort_key(entry):
            name = self.search_key(entry[0][0])
            return alphabetical and not ("a" <= name[:1] <= "z"), name

        for (name, target), children in sorted(branch.items(), key=sort_key):
            if alphabetical:
                first = self.search_key(name)[:1].upper()
                initial = first if "A" <= first <= "Z" else "#"
                if initial != letter:
                    section = QTreeWidgetItem(parent, [initial])
                    section.setFlags(Qt.ItemIsEnabled)
                    section.setForeground(0, self.section_color)
                    font = self.apps.font()
                    font.setPointSize(9)
                    font.setBold(True)
                    section.setFont(0, font)
                    letter = initial
            if children is None:
                self.add_app(section, name, target)
            else:
                folder = QTreeWidgetItem(section, [name])
                folder.setIcon(0, self.folder_icon)
                child_path = (*path, name)
                folder.setData(0, Qt.UserRole + 1, list(child_path))
                self.add_tree(folder, children, path=child_path)
                folder.setExpanded(child_path in self.expanded_folders)
            if alphabetical:
                section.setExpanded(True)

    def add_app(self, parent, name, target, folder=()):
        item = QTreeWidgetItem(parent, [name])
        item.setData(0, Qt.UserRole, target)
        item.setToolTip(0, " › ".join((*folder, name)))
        item.setIcon(0, self.generic_icon)
        return item

    def decorate_visible(self, *_args):
        if not self.icon_refresh.isActive():
            self.icon_refresh.start()

    def remember_folder(self, item, expanded):
        path = item.data(0, Qt.UserRole + 1)
        if path:
            if expanded:
                self.expanded_folders.add(tuple(path))
            else:
                self.expanded_folders.discard(tuple(path))

    def load_visible_icons(self):
        if self.icon_batch is not None:
            while not self.icon_batch.empty():
                target, image = self.icon_batch.get_nowait()
                if target is None:
                    self.icon_batch = None
                    break
                self.icons[target] = (self.generic_icon if image.isNull()
                                      else QIcon(QPixmap.fromImage(image)))
                self.icons.move_to_end(target)
                if len(self.icons) > self.MAX_CACHED_ICONS:
                    self.icons.popitem(last=False)
        if not self.isVisible():
            if self.icon_batch is not None:
                self.decorate_visible()
            return
        viewport = self.apps.viewport()
        targets = []
        visible = {}
        item = self.apps.itemAt(QPoint(3, 0))
        while item is not None and self.apps.visualItemRect(item).top() < viewport.height():
            target = item.data(0, Qt.UserRole)
            if target in self.icons:
                icon = self.icons[target]
                self.icons.move_to_end(target)
                if item.icon(0).cacheKey() != icon.cacheKey():
                    item.setIcon(0, icon)
                visible[id(item)] = item
            elif target:
                targets.append(target)
            item = self.apps.itemBelow(item)
        for key, old in self.decorated_items.items():
            if key not in visible:
                old.setIcon(0, self.generic_icon)
        self.decorated_items = visible
        if targets and self.icon_batch is None:
            self.icon_batch = queue.SimpleQueue()
            threading.Thread(target=self.collect_icons,
                             args=(tuple(dict.fromkeys(targets)), self.icon_batch), daemon=True).start()
        if self.icon_batch is not None:
            self.decorate_visible()

    @classmethod
    def collect_icons(cls, targets, result):
        initialized = ole32.CoInitializeEx(None, 2)
        try:
            for target in targets:
                try:
                    image = cls.app_icon_image(target)
                except (OSError, RuntimeError):
                    image = QImage()
                result.put((target, image))
        finally:
            if initialized >= 0:
                ole32.CoUninitialize()
            result.put((None, None))

    @staticmethod
    def app_icon_image(target):
        if target.lower().endswith(".lnk"):
            app_path, icon_path, index = read_shortcut(target)
            for source, icon_index in ((icon_path, index), (app_path, 0)):
                if source and not is_network(source) and source.lower().endswith(ICON_FILE_EXTS):
                    icon = extract_icon_image(source, icon_index, 32)
                    if not icon.isNull():
                        return icon
        return shell_item_icon_image(target)

    def select_search_result(self):
        item = self.apps.currentItem()
        if item is None and self.search.text():
            item = self.apps.topLevelItem(0)
        if item is not None:
            self.activate_entry(item)

    def activate_entry(self, item, _column=0):
        target = item.data(0, Qt.UserRole)
        if not target:
            if item.flags() & Qt.ItemIsSelectable and item.childCount():
                item.setExpanded(not item.isExpanded())
            return
        try:
            os.startfile(target)
        except OSError:
            self.status.setText(f"Não foi possível abrir {item.text(0)}.")
            self.status.show()
        else:
            self.close()

    def open_windows_start(self):
        self.close()
        control, escape = 0x11, 0x1B
        user32.keybd_event(control, 0, 0, START_KEY_TAG)
        user32.keybd_event(escape, 0, 0, START_KEY_TAG)
        user32.keybd_event(escape, 0, KEYEVENTF_KEYUP, START_KEY_TAG)
        user32.keybd_event(control, 0, KEYEVENTF_KEYUP, START_KEY_TAG)

    def open_location(self, target):
        try:
            os.startfile(target)
        except OSError:
            self.status.setText("Não foi possível abrir o atalho.")
            self.status.show()
        else:
            self.close()

    def open_power_menu(self):
        button = self.power_button
        menu = QMenu(self)
        menu.setStyleSheet(self.dock.pal["menu"])
        menu.addAction("Bloquear", lambda: self.power_action(None))
        menu.addSeparator()
        menu.addAction("Desligar", lambda: self.power_action("/s"))
        menu.addAction("Reiniciar", lambda: self.power_action("/r"))
        anchor = button.mapToGlobal(QPoint(0, 0))
        menu.exec(anchor - QPoint(0, menu.sizeHint().height() + 4))

    def power_action(self, operation):
        self.close()
        if operation is None:
            user32.LockWorkStation()
        else:
            QProcess.startDetached("shutdown.exe", [operation, "/t", "0"])

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress and watched in (self.search, self.apps):
            key = event.key()
            if key == Qt.Key_Escape:
                self.close()
                return True
            if watched is self.search and key in (Qt.Key_Down, Qt.Key_Up):
                item = self.apps.currentItem()
                step = self.apps.itemBelow if key == Qt.Key_Down else self.apps.itemAbove
                if item is None:
                    index = (self.apps.topLevelItemCount() - 1
                             if key == Qt.Key_Up and self.search.text().strip() else 0)
                    item = self.apps.topLevelItem(index)
                else:
                    item = step(item)
                while item is not None and not item.flags() & Qt.ItemIsSelectable:
                    item = step(item)
                if item is not None:
                    self.apps.setCurrentItem(item)
                    self.apps.scrollToItem(item)
                return True
        return super().eventFilter(watched, event)

    def hideEvent(self, event):
        self.dock.menu_open = False
        for item in self.decorated_items.values():
            item.setIcon(0, self.generic_icon)
        self.decorated_items.clear()
        super().hideEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(self.surface_color)
        painter.setPen(QPen(self.border_color, 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 13, 13)


class Dock(QWidget):
    def __init__(self, group: DockGroup | None = None, screen=None) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setWindowTitle(APP_TITLE)

        if group is None:  # dock avulso (autoteste, geração de prints)
            group = DockGroup()
            group.docks.append(self)
        self.group = group
        self.screen_ref = screen or QGuiApplication.primaryScreen()
        self.screen_geometry_changed = (
            self.screen_ref.geometryChanged if DESKTOP_MODE else self.screen_ref.availableGeometryChanged
        )
        self.setScreen(self.screen_ref)
        self.pressed_pixmap: QPixmap | None = None  # versão escurecida, só durante o clique

        self.theme_name = self.resolve_theme()
        self.blur: BlurBehind | None = BlurBehind() if self.cfg["blur"] else None
        self.forced: str | None = None    # atalho de teclado: "show", "hide" ou None
        self.hotkey_ok: bool | None = None
        self.focus_hotkey_ok = False
        self.keyboard_item = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.drop_hint: tuple[int, str] | None = None  # (item, texto) ao soltar arquivo em cima
        self.banner: dict | None = None   # aviso acima do dock (atualização)
        self.banner_rect = QRectF()
        self.update_info: dict | None = None
        self.update_result: dict | None = None
        self.update_manual = False
        self.update_thread: threading.Thread | None = None
        self.update_last = time.monotonic() - UPDATE_EVERY_SECS + 20  # 1ª checagem ~20 s após abrir
        self.ticks = 0

        self.hover = 0.0        # 0 = sem ampliação, 1 = ampliação total
        self.mouse_x = 0.0      # posição x do mouse (coordenadas locais)
        self.reveal = 1.0       # 0 = escondido (auto-ocultar), 1 = visível
        self.last_hover = 0.0
        self.last_state = None

        self.press_index: int | None = None
        self.press_pos = QPointF()
        self.dragging = False
        self.drag_pos = QPointF()
        self.drag_remove = False
        self.menu_open = False
        self.start_launcher = None
        self.preview = None
        self.preview_item = None
        self.preview_since = 0.0

        self.label_font = QApplication.font()
        self.label_font.setPointSize(9)
        self.label_font.setWeight(QFont.Weight.Medium)

        self.anim_timer = QTimer(self, interval=16, timeout=self.tick)
        self.anim_timer.start()
        self.slow_timer = QTimer(self, interval=1000, timeout=self.slow_tick)
        self.slow_timer.start()
        # Ícones que dependem da rede (\\servidor\...) e falharam: checados de
        # novo em segundo plano, para um servidor lento não travar o dock.
        self.net_thread: threading.Thread | None = None
        self.net_found: set[str] | None = None
        self.net_last = time.monotonic()
        self.cover: str | None = None   # "fullscreen", "maximized" ou None
        self.cover_timer = QTimer(self, interval=300, timeout=self.check_cover)
        self.cover_timer.start()

        self.screen_geometry_changed.connect(self.reposition)
        self.reposition()
        # Terminada a carga (ícones em alta resolução, leitura de atalhos...),
        # devolve ao Windows a memória que só serviu para isso.
        QTimer.singleShot(3000, trim_memory)

    # ---- dados compartilhados entre os docks (ver DockGroup) -------------
    @property
    def cfg(self) -> dict:
        return self.group.cfg

    @property
    def items(self) -> list:
        return self.group.items

    @property
    def provider(self) -> QFileIconProvider:
        return self.group.provider

    @property
    def icon_px(self) -> int:
        return self.group.icon_px

    @property
    def is_leader(self) -> bool:
        return self.group.leader is self

    def shutdown(self) -> None:
        """Fecha este dock de vez (troca de monitores ou opção de telas)."""
        self.close_previews()
        for timer in (self.anim_timer, self.slow_timer, self.cover_timer):
            timer.stop()
        if self.hotkey_ok:
            user32.UnregisterHotKey(int(self.winId()), HOTKEY_ID)
            self.hotkey_ok = None
        if self.focus_hotkey_ok:
            user32.UnregisterHotKey(int(self.winId()), DOCK_FOCUS_HOTKEY_ID)
            self.focus_hotkey_ok = False
        if self.blur is not None:
            self.blur.close()
            self.blur.deleteLater()
            self.blur = None
        try:
            self.screen_geometry_changed.disconnect(self.reposition)
        except (RuntimeError, TypeError):
            pass
        self.close()
        self.deleteLater()

    # ---- tema e desfoque -----------------------------------------------
    def resolve_theme(self) -> str:
        theme = self.cfg.get("theme", "auto")
        if theme in THEMES:
            return theme
        return "light" if windows_uses_light_theme() else "dark"

    @property
    def pal(self) -> dict:
        return THEMES[self.theme_name]

    def sync_blur(self, panel: QRectF) -> None:
        """Mantém a janela de desfoque exatamente atrás do painel (que muda de
        largura com a ampliação e desce ao se esconder)."""
        if self.blur is None:
            return
        # Alguns compositores desenham o desfoque até o retângulo da janela
        # auxiliar mesmo com uma região arredondada. Inscreva esse retângulo
        # no painel para que ele nunca ultrapasse a curva dos cantos.
        inset = math.ceil(PANEL_RADIUS * (1 - 1 / math.sqrt(2))) + 1
        visible = panel.adjusted(inset, inset, -inset, -inset).intersected(QRectF(self.rect()))
        if not self.isVisible() or self.reveal < 0.02 or visible.height() < 8:
            if self.blur.isVisible():
                self.blur.hide()
            return
        left, top = math.ceil(visible.left()), math.ceil(visible.top())
        right, bottom = math.floor(visible.right()), math.floor(visible.bottom())
        self.blur.place(QRect(self.mapToGlobal(QPoint(left, top)), QSize(right - left, bottom - top)),
                        int(self.winId()))

    # ---- atalho de teclado (mostrar/esconder) ---------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.hotkey_ok is None and self.is_leader:  # o atalho é global: um registro só
            self.register_hotkey()
            mods, key = parse_hotkey(DOCK_FOCUS_SHORTCUT)
            self.focus_hotkey_ok = bool(user32.RegisterHotKey(
                int(self.winId()), DOCK_FOCUS_HOTKEY_ID, mods | MOD_NOREPEAT, key))

    def register_hotkey(self) -> None:
        parsed = parse_hotkey(self.cfg.get("hotkey", ""))
        if not parsed:
            self.hotkey_ok = False
            return
        mods, key = parsed
        self.hotkey_ok = bool(user32.RegisterHotKey(int(self.winId()), HOTKEY_ID, mods | MOD_NOREPEAT, key))
        if not self.hotkey_ok:
            log(f"atalho {self.cfg.get('hotkey')} já está em uso por outro programa")

    def nativeEvent(self, event_type, message):
        if bytes(event_type.data()) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == DOCK_FOCUS_HOTKEY_ID:
                self.group.focus_dock()
                return True, 0
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.toggle_visibility()
                return True, 0
        return False, 0

    def enter_keyboard_mode(self):
        item = next((it for it in self.items if not it.is_sep), None)
        if item is None:
            return
        self.close_previews()
        if self.keyboard_item is None:
            self.keyboard_previous_window = user32.GetForegroundWindow()
        self.keyboard_item = item
        self.setAccessibleName(clean_name(item.name))
        self.reveal = 1.0
        self.show()
        self.raise_()
        user32.SetForegroundWindow(int(self.winId()))
        self.activateWindow()
        self.setFocus(Qt.ShortcutFocusReason)
        self.update()

    def leave_keyboard_mode(self):
        if self.keyboard_item is not None:
            self.keyboard_item = None
            self.setAccessibleName(APP_TITLE)
            self.update()

    def focusOutEvent(self, event):
        if not self.menu_open:
            self.leave_keyboard_mode()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if self.keyboard_item is None:
            return super().keyPressEvent(event)
        choices = [it for it in self.items if not it.is_sep]
        if not choices:
            self.leave_keyboard_mode()
            return
        index = choices.index(self.keyboard_item) if self.keyboard_item in choices else 0
        key = event.key()
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End):
            index = {Qt.Key_Left: max(0, index - 1), Qt.Key_Right: min(len(choices) - 1, index + 1),
                     Qt.Key_Home: 0, Qt.Key_End: len(choices) - 1}[key]
            self.keyboard_item = choices[index]
            self.setAccessibleName(clean_name(self.keyboard_item.name))
            self.update()
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.leave_keyboard_mode()
            self.activate(choices[index])
        elif key == Qt.Key_Escape:
            self.leave_keyboard_mode()
            previous = self.keyboard_previous_window
            if previous and previous != int(self.winId()):
                user32.SetForegroundWindow(previous)
        elif key == Qt.Key_Menu or (key == Qt.Key_F10 and event.modifiers() & Qt.ShiftModifier):
            item_index = self.items.index(choices[index])
            rect = self.layout()[1][item_index]
            point = QPoint(round(rect.center().x()), round(rect.top()))
            menu = self.build_context_menu(point, item_index)
            self.menu_open = True
            try:
                selected = menu.exec(self.mapToGlobal(point) - QPoint(0, menu.sizeHint().height()))
            finally:
                self.menu_open = False
                menu.deleteLater()
            if selected is None and self.isActiveWindow():
                self.setFocus(Qt.ShortcutFocusReason)
            else:
                self.leave_keyboard_mode()
        else:
            return super().keyPressEvent(event)
        event.accept()

    def toggle_visibility(self) -> None:
        """Ctrl+Alt+D: esconde todos os docks se este estiver aparecendo; senão mostra todos."""
        showing = self.isVisible() and self.reveal > 0.5 and self.forced != "hide"
        self.group.set_forced("hide" if showing else "show")

    # ---- atualizações ----------------------------------------------------
    def check_updates(self, manual: bool = False) -> None:
        source = update_source(self.cfg)
        if not source or (self.update_thread and self.update_thread.is_alive()):
            return
        self.update_manual = manual
        self.update_last = time.monotonic()

        def work() -> None:
            self.update_result = fetch_latest(source) or {"version": ""}

        self.update_thread = threading.Thread(target=work, daemon=True)
        self.update_thread.start()

    def handle_update_result(self) -> bool:
        result, self.update_result = self.update_result, None
        if result is None:
            return False
        newer = result["version"] and version_tuple(result["version"]) > version_tuple(APP_VERSION)
        if newer:
            first = self.update_info is None or self.update_info["version"] != result["version"]
            self.update_info = result
            if first or self.update_manual:
                self.show_banner(f"Nova versão {result['version']} do Pingo Dock — clique para atualizar",
                                 25, self.start_update)
        elif self.update_manual:
            text = (f"O Pingo Dock está atualizado (versão {APP_VERSION})" if result["version"]
                    else "Não foi possível verificar atualizações (veja o dock.log)")
            self.show_banner(text, 5)
        return True

    def start_update(self) -> None:
        if self.update_info:
            self.show_banner("Atualizando o Pingo Dock…", 30)
            run_update_installer(self.update_info["setup"])

    def show_banner(self, text: str, seconds: float, action=None) -> None:
        self.banner = {"text": text, "until": time.monotonic() + seconds, "action": action}
        self.update()

    # ---- parâmetros -----------------------------------------------------
    @property
    def S(self) -> float:
        size = float(self.cfg["icon_size"])
        if not DESKTOP_MODE:
            return size
        separators = sum(item.is_sep for item in self.items)
        icons = len(self.items) - separators
        gaps = max(0, len(self.items) - 1) * self.G
        room = self.screen_ref.geometry().width() - gaps - separators * SEP_SLOT - 2 * PAD_X
        expanded = max(1, icons + 3 * (self.M - 1))
        return min(size, max(16, room / expanded))

    @property
    def G(self) -> float:
        return float(self.cfg["spacing"])

    @property
    def M(self) -> float:
        return float(self.cfg["magnification"]) if self.cfg["magnify"] else 1.0

    # ---- geometria ------------------------------------------------------
    def base_widths(self) -> list[float]:
        return [SEP_SLOT if it.is_sep else self.S for it in self.items]

    def reposition(self) -> None:
        S, G, M = self.S, self.G, float(self.cfg["magnification"])
        widths = self.base_widths() or [S]
        base_w = sum(widths) + (len(widths) - 1) * G + 2 * PAD_X
        w = int(base_w + S * (M - 1) * 3 + 240)
        h = int(S * M + S * 0.5 + PAD_BOTTOM + MARGIN_BOTTOM + 48)
        area = self.screen_ref.geometry() if DESKTOP_MODE else self.screen_ref.availableGeometry()
        w = min(w, area.width())
        self.setGeometry(area.x() + (area.width() - w) // 2, area.bottom() - h + 1, w, h)
        self.update()

    def layout(self) -> tuple[QRectF, list[QRectF]]:
        """Retângulo do painel e de cada ícone, já com ampliação e pulo."""
        S, G, M = self.S, self.G, self.M
        n = len(self.items)
        panel_h = S + PAD_TOP + PAD_BOTTOM
        hidden_shift = (1 - self.reveal) * (panel_h + MARGIN_BOTTOM + 4)
        panel_bottom = self.height() - MARGIN_BOTTOM + hidden_shift
        cx = self.width() / 2

        widths = self.base_widths()
        base_w = sum(widths) + max(n - 1, 0) * G + 2 * PAD_X
        x = cx - base_w / 2 + PAD_X
        sizes = []
        for item, w in zip(self.items, widths):
            center = x + w / 2
            x += w + G
            if item.is_sep:
                sizes.append(w)
                continue
            d = abs(self.mouse_x - center) / (S + G)
            f = math.cos(d / RADIUS * math.pi / 2) ** 2 if d < RADIUS else 0.0
            sizes.append(S * (1 + (M - 1) * self.hover * f))

        total = sum(sizes) + max(n - 1, 0) * G + 2 * PAD_X if n else S + 2 * PAD_X
        left = cx - total / 2
        panel = QRectF(left, panel_bottom - panel_h, total, panel_h)

        rects = []
        x = left + PAD_X
        now = time.monotonic()
        for item, s in zip(self.items, sizes):
            if item.is_sep:
                rects.append(QRectF(x, panel_bottom - PAD_BOTTOM - S, s, S))
            else:
                bounce = item.bounce_offset(now, S)
                rects.append(QRectF(x, panel_bottom - PAD_BOTTOM - s - bounce, s, s))
            x += s + G
        return panel, rects

    def insert_index(self, x: float) -> int:
        """Posição na lista correspondente à coordenada x (para inserir algo ali)."""
        _, rects = self.layout()
        return sum(1 for r in rects if r.center().x() < x)

    def hit(self, pos: QPointF) -> int | None:
        panel, rects = self.layout()
        if pos.y() > panel.bottom() + 2:
            return None
        for i, r in enumerate(rects):
            if r.left() - self.G / 2 <= pos.x() < r.right() + self.G / 2 and pos.y() >= min(r.top(), panel.top()) - 4:
                return i
        return None

    # ---- animação -------------------------------------------------------
    def tick(self) -> None:
        if not self.isVisible():
            return
        gp = QCursor.pos()
        p = QPointF(self.mapFromGlobal(gp))
        panel, rects = self.layout()
        top = min([r.top() for r in rects] + [panel.top()])
        inside = self.dragging or (
            panel.left() <= p.x() <= panel.right() and top - 4 <= p.y() <= self.height()
        )
        now = time.monotonic()
        if DESKTOP_MODE:
            inside |= self.track_previews(gp, p, rects, inside, now)
        if inside:
            self.last_hover = now

        if not self.menu_open:
            if inside:
                self.mouse_x = p.x()
            target = 1.0 if inside else 0.0
            self.hover += (target - self.hover) * 0.22
            if abs(target - self.hover) < 0.002:
                self.hover = target

        # Atalho de teclado manda: escondido até apertar de novo, ou visível.
        # Senão, esconde quando "ocultar automaticamente" está ligado ou quando uma
        # janela maximizada ocupa a tela; reaparece ao encostar o mouse na borda.
        if self.keyboard_item is not None:
            target_reveal = 1.0
        elif self.forced == "hide" and not self.menu_open:
            target_reveal = 0.0
        elif self.forced == "show":
            target_reveal = 1.0
        elif self.cfg["autohide"] or self.cover == "maximized":
            at_edge = (
                self.height() - 3 <= p.y() <= self.height() + 1 and 0 <= p.x() <= self.width()
            )
            want = inside or at_edge or self.menu_open or now - self.last_hover < 0.6
            target_reveal = 1.0 if want else 0.0
        else:
            target_reveal = 1.0
        self.reveal += (target_reveal - self.reveal) * 0.2
        if abs(target_reveal - self.reveal) < 0.002:
            self.reveal = target_reveal

        bouncing = any(it.bounce_t0 is not None for it in self.items)
        state = (
            round(self.hover, 3), round(self.mouse_x, 1), round(self.reveal, 3),
            self.dragging, self.drag_remove, round(self.drag_pos.x()), round(self.drag_pos.y()),
            self.hit(p) if self.hover > 0.5 else None, self.press_index,
            self.drop_hint, self.banner is not None, self.theme_name,
        )
        if bouncing or state != self.last_state:
            self.last_state = state
            self.update()
            self.sync_blur(self.layout()[0])

    def track_previews(self, global_pos, local_pos, rects, inside, now):
        if self.keyboard_item is not None or self.menu_open or self.press_index is not None or self.dragging or self.forced == "hide":
            self.close_previews()
            return False
        if self.preview is not None:
            if self.preview.options_open:
                return True
            windows = tuple(self.group.app_window_handles.get(self.preview.item, ()))
            if not windows:
                self.close_previews()
            else:
                if set(windows) != set(self.preview.windows):
                    if self.preview.geometry().contains(global_pos):
                        self.preview_since = now
                    self.preview.refresh_windows(windows)
                self.preview.track_peek(global_pos, now)
                if self.preview.geometry().contains(global_pos):
                    self.preview_since = now
                    return True
        index = self.hit(local_pos) if inside and self.reveal > 0.85 else None
        item = self.items[index] if index is not None else None
        windows = tuple(self.group.app_window_handles.get(item, ()))
        if self.preview is not None:
            if item is self.preview.item:
                self.preview_since = now
                return False
            if not windows and now - self.preview_since < 0.24:
                return True
            self.close_previews()
        candidate = item if windows else None
        if candidate is not self.preview_item:
            self.preview_item, self.preview_since = candidate, now
        elif candidate is not None and now - self.preview_since >= 0.45:
            rect = rects[index]
            anchor = self.mapToGlobal(QPoint(round(rect.center().x()), round(rect.top())))
            self.preview = WindowPreviews(self, item, windows, anchor)
            self.update()
        return False

    def close_previews(self):
        self.preview_item = None
        if self.preview is not None:
            self.preview.clear_thumbnails()
            self.preview.close()
            self.preview.deleteLater()
            self.preview = None
            self.update()

    @staticmethod
    def named_windows() -> list[tuple[int, int, str]]:
        """(janela, pid, nome do executável) de todas as janelas de app. Só consulta
        os processos que têm janela — nada de varrer a lista inteira de processos."""
        names: dict[int, str] = {}
        result = []
        for hwnd, pid in app_windows():
            if pid not in names:
                names[pid] = process_name(pid)
            result.append((hwnd, pid, names[pid]))
        return result

    def item_windows(self, item: DockItem, windows: list[tuple[int, int, str]]) -> list[int]:
        if item.transient:
            return self.group.app_window_handles.get(item, [])
        return [hwnd for hwnd, _pid, name in windows if name in item.match_names]

    def sync_open_apps(self, windows):
        pinned = [item for item in self.items if isinstance(item, DockItem) and not item.transient]
        existing = {item.path.casefold(): item for item in self.items if item.transient}
        paths = {}
        grouped = {}
        for hwnd, pid, name in windows:
            ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            if ex_style & WS_EX_LAYERED and ex_style & 0x20:  # WS_EX_TRANSPARENT
                continue
            if any(name in item.match_names for item in pinned):
                continue
            if pid not in paths:
                paths[pid] = self.group.window_paths.get(pid) or process_image_path(pid)
            path = paths[pid]
            if path:
                grouped.setdefault(path.casefold(), (path, pid, []))[2].append(hwnd)
        self.group.window_paths = paths
        active = []
        handles = {}
        for key in [key for key in existing if key in grouped] + [key for key in grouped if key not in existing]:
            path, pid, hwnds = grouped[key]
            item = existing.get(key)
            if item is None:
                name = "Instalador" if Path(path).name.casefold() == "setup.tmp" else Path(path).stem
                item = DockItem(name, path, self.provider, self.icon_px,
                                launch_uri=packaged_launch_uri(pid))
                item.transient = True
            handles[item] = hwnds
            active.append(item)
        self.group.app_window_handles = handles
        if active == list(existing.values()):
            return False
        if any(dock.press_index is not None or dock.menu_open for dock in self.group.docks):
            return False
        self.items[:] = [item for item in self.items if not item.transient]
        edge = self.items.index(self.group.end_separator)
        self.items[edge:edge] = active
        self.group.refresh()
        return True

    def slow_tick(self) -> None:
        self.ticks += 1
        changed = False
        theme = self.resolve_theme()
        if theme != self.theme_name:
            self.theme_name = theme
            changed = True
        if self.blur is not None:
            self.blur.stay_below(int(self.winId()))  # outra janela "sempre visível" pode ter entrado no meio
        if self.banner and time.monotonic() > self.banner["until"]:
            self.banner = None
            changed = True
        if changed:
            self.update()
        if self.is_leader and self.leader_tick():
            self.group.update_all()  # itens são compartilhados: todos os docks redesenham

    def leader_tick(self) -> bool:
        """Trabalho feito uma vez só, pelo dock da tela principal, para todos."""
        windows = self.named_windows()
        changed = self.sync_open_apps(windows) if DESKTOP_MODE else False
        if DESKTOP_MODE:
            for item in self.items:
                if isinstance(item, DockItem) and not item.transient:
                    self.group.app_window_handles[item] = self.item_windows(item, windows)
        for it in self.items:
            if it.is_sep:
                continue
            # "Aberto" = tem pelo menos uma janela (como a barra de tarefas do Windows).
            # Assim o Explorador (que sempre roda por trás) e apps que ficam em
            # segundo plano, como o Edge, só ganham bolinha com janela aberta.
            app_windows = self.item_windows(it, windows)
            running = bool(app_windows)
            if running != it.running:
                it.running = running
                changed = True
            if isinstance(it, DockItem) and it.installer:
                progress = next((value for hwnd in app_windows
                                 if (value := installer_progress(hwnd)) is not None), None)
                if progress != it.progress:
                    it.progress = progress
                    changed = True
            if it.learn_t0 is not None:
                changed |= self.try_learn(it, running, windows)
        changed |= self.retry_network_icons()
        if self.ticks % 3 == 0:
            for it in self.items:
                if it.is_trash:
                    full = recycle_bin_count() > 0
                    if full != it.full:
                        it.full = full
                        changed = True
        if time.monotonic() - self.update_last >= UPDATE_EVERY_SECS:
            self.check_updates()
        changed |= self.handle_update_result()
        return changed

    def retry_network_icons(self) -> bool:
        """Recarrega os ícones cujos arquivos/pastas de rede voltaram a responder."""
        found, self.net_found = self.net_found, None
        changed = False
        if found:
            for it in self.items:
                if not it.is_sep and it.icon_missing and any(p in found for p in it.icon_missing):
                    it.reload_icon(self.provider, self.icon_px)
                    changed = True
        pending = {p for it in self.items if not it.is_sep for p in it.icon_missing}
        busy = self.net_thread is not None and self.net_thread.is_alive()
        if pending and not busy and time.monotonic() - self.net_last >= NET_RETRY_SECS:
            self.net_last = time.monotonic()

            def check(paths=sorted(pending)) -> None:
                self.net_found = {p for p in paths if os.path.exists(p)}

            self.net_thread = threading.Thread(target=check, daemon=True)
            self.net_thread.start()
        return changed

    def check_cover(self) -> None:
        if self.menu_open:
            return
        state, cause = cover_state(int(self.winId()))
        if state != self.cover:
            fg = user32.GetForegroundWindow()
            log(f"estado: {self.cover} -> {state} | causa: {describe_window(cause)} | "
                f"primeiro plano: {describe_window(fg)}")
            if self.forced == "show":
                self.forced = None  # "mostrar" pelo atalho vale até a tela mudar
        self.cover = state
        hide_for_fullscreen = self.cover == "fullscreen" and self.forced != "show" and self.keyboard_item is None
        if hide_for_fullscreen and self.isVisible():
            self.close_previews()
            self.hide()
            if self.blur is not None:
                self.blur.hide()
        elif not hide_for_fullscreen and not self.isVisible():
            self.reveal = 0.0 if self.cover == "maximized" else 1.0
            self.show()

    def try_learn(self, item: DockItem, running: bool, windows: list[tuple[int, int, str]]) -> bool:
        """Depois de abrir um app que ainda não reconhecemos (ex.: um lançador que
        abre outro .exe), a primeira janela nova em primeiro plano é dele."""
        if running or time.monotonic() - item.learn_t0 > LEARN_TIME:
            item.learn_t0 = None
            return False
        fg = user32.GetForegroundWindow()
        found = next(((p, n) for h, p, n in windows if h == fg), None)
        if found is None or fg in item.learn_before or found[0] == os.getpid() or not found[1]:
            return False
        name = found[1]
        item.learn_t0 = None
        if name in NEVER_LEARN:
            return False
        item.learn(name)
        item.running = True
        self.save()
        return True

    # ---- desenho --------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        panel, rects = self.layout()

        pal = self.pal
        # painel "de vidro" (mais transparente quando há desfoque atrás)
        path = QPainterPath()
        path.addRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), PANEL_RADIUS, PANEL_RADIUS)
        p.fillPath(path, pal["panel_blur"] if self.blur is not None else pal["panel"])
        grad = QLinearGradient(panel.topLeft(), panel.bottomLeft())
        grad.setColorAt(0, pal["grad_top"])
        grad.setColorAt(1, pal["grad_bottom"])
        p.fillPath(path, grad)
        p.setPen(QPen(pal["border"], 1))
        p.drawPath(path)

        # destaque do ícone que vai receber os arquivos arrastados
        if self.drop_hint is not None and self.drop_hint[0] < len(rects):
            r = rects[self.drop_hint[0]].adjusted(-4, -4, 4, 4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal["highlight"])
            p.drawRoundedRect(r, r.width() * 0.24, r.width() * 0.24)

        # ícones
        for i, (item, r) in enumerate(zip(self.items, rects)):
            if self.dragging and i == self.press_index:
                continue  # espaço vazio onde o ícone arrastado vai cair
            if item.is_sep:
                self.draw_separator(p, r.center().x(), panel.top() + 9, panel.bottom() - 9)
                continue
            if item is self.keyboard_item:
                p.save()
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(pal["dot"], 2))
                p.drawRoundedRect(r.adjusted(-4, -4, 4, 4), 7, 7)
                p.restore()
            pressed = i == self.press_index and not self.dragging
            pm = self.pressed_pixmap if pressed and self.pressed_pixmap else item.pixmap
            p.drawPixmap(r, pm, QRectF(pm.rect()))
            if item.is_folder:
                self.draw_folder_name(p, r, clean_name(item.name))
            if isinstance(item, DockItem) and item.progress is not None:
                width = max(16, r.width() * 0.72)
                track = QRectF(r.center().x() - width / 2, panel.bottom() - 7, width, 4)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 155))
                p.drawRoundedRect(track, 2, 2)
                fill = track.width() * item.progress / 100
                if fill > 0:
                    p.setBrush(QColor("#81d9ff"))
                    p.drawRoundedRect(QRectF(track.x(), track.y(), fill, track.height()), min(2, fill / 2), 2)
            elif item.running:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(pal["dot"])
                p.drawEllipse(QPointF(r.center().x(), panel.bottom() - 5), 2.4, 2.4)

        # ícone sendo arrastado
        if self.dragging and self.press_index is not None:
            item = self.items[self.press_index]
            s = self.S * (1.15 if not self.drag_remove else 0.9)
            r = QRectF(self.drag_pos.x() - s / 2, self.drag_pos.y() - s / 2, s, s)
            p.setOpacity(0.45 if self.drag_remove else 0.9)
            if item.is_sep:
                self.draw_separator(p, r.center().x(), r.top(), r.bottom())
            else:
                p.drawPixmap(r, item.pixmap, QRectF(item.pixmap.rect()))
            p.setOpacity(1.0)
            if self.drag_remove:
                self.draw_label(p, "Remover", r.center().x(), r.top() - 6)

        # arquivo arrastado em cima de um app/Lixeira: diz o que vai acontecer
        elif self.drop_hint is not None and self.drop_hint[0] < len(rects):
            r = rects[self.drop_hint[0]]
            self.draw_label(p, self.drop_hint[1], r.center().x(), r.top() - 8)

        # nome do app sob o mouse
        elif self.hover > 0.6 and not self.menu_open and self.preview is None:
            idx = self.hit(QPointF(self.mapFromGlobal(QCursor.pos())))
            if idx is not None and not self.items[idx].is_sep:
                r = rects[idx]
                item = self.items[idx]
                text = clean_name(item.name)
                if isinstance(item, DockItem) and item.progress is not None:
                    text += " · " + QLocale("pt_BR").toString(item.progress, "f", 1) + "%"
                self.draw_label(p, text, r.center().x(), r.top() - 8)

        # aviso (ex.: versão nova) — some enquanto o mouse está sobre o dock
        self.banner_rect = QRectF()
        if self.banner and self.hover < 0.3 and not self.dragging:
            self.banner_rect = self.draw_label(p, self.banner["text"], panel.center().x(),
                                               panel.top() - 10, accent=self.banner["action"] is not None)
        p.end()

    @staticmethod
    def draw_folder_name(p: QPainter, r: QRectF, name: str) -> None:
        """Etiqueta com o nome da pasta na parte de baixo do ícone (cresce junto
        com a ampliação)."""
        font = QApplication.font()
        font.setPixelSize(max(8, round(r.width() * 0.19)))
        font.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetricsF(font)
        max_w = r.width() * 0.96
        text = fm.elidedText(name, Qt.TextElideMode.ElideRight, max_w - 6)
        w = min(fm.horizontalAdvance(text) + 8, max_w)
        h = fm.height() + 1
        pill = QRectF(r.center().x() - w / 2, r.bottom() - h - r.height() * 0.06, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 165))
        p.drawRoundedRect(pill, h / 2.6, h / 2.6)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

    def draw_separator(self, p: QPainter, x: float, top: float, bottom: float) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.pal["sep"])
        p.drawRoundedRect(QRectF(x - 0.75, top, 1.5, bottom - top), 0.75, 0.75)

    def draw_label(self, p: QPainter, text: str, cx: float, bottom: float, accent: bool = False) -> QRectF:
        pal = self.pal
        fm = QFontMetricsF(self.label_font)
        w = fm.horizontalAdvance(text) + 24
        h = fm.height() + 10
        x = min(max(cx - w / 2, 2), self.width() - w - 2)
        r = QRectF(x, max(bottom - h, 1), w, h)
        path = QPainterPath()
        path.addRoundedRect(r, 7, 7)
        p.fillPath(path, QColor(59, 111, 224, 240) if accent else pal["label_bg"])
        p.setPen(QPen(pal["label_border"], 1))
        p.setBrush(Qt.BrushStyle.NoBrush)  # só a borda (senão herda o pincel do último desenho)
        p.drawPath(path)
        p.setPen(QColor(255, 255, 255) if accent else pal["label_text"])
        p.setFont(self.label_font)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        return r

    # ---- mouse ----------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        if (e.button() == Qt.MouseButton.LeftButton and self.banner
                and self.banner_rect.contains(e.position())):
            action, self.banner = self.banner["action"], None
            if action:
                action()
            self.update()
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self.press_index = self.hit(e.position())
            self.press_pos = e.position()
            self.dragging = False
            item = self.items[self.press_index] if self.press_index is not None else None
            self.pressed_pixmap = darkened(item.pixmap) if item and not item.is_sep else None
            self.update()

    def mouseMoveEvent(self, e) -> None:
        if self.press_index is None or not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        if self.items[self.press_index].fixed or self.items[self.press_index].transient:
            return
        pos = e.position()
        if not self.dragging and (pos - self.press_pos).manhattanLength() > 8:
            self.dragging = True
        if not self.dragging:
            return
        self.drag_pos = pos
        panel, rects = self.layout()
        self.drag_remove = pos.y() < panel.top() - self.S * 1.3
        if not self.drag_remove:
            target = min(range(len(rects)), key=lambda i: abs(rects[i].center().x() - pos.x()))
            target = max(self.pin_start(), min(target, self.pin_end() - 1))
            if target != self.press_index:
                self.items.insert(target, self.items.pop(self.press_index))
                self.press_index = target
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        idx, self.press_index = self.press_index, None
        self.pressed_pixmap = None
        if self.dragging:
            self.dragging = False
            if self.drag_remove and idx is not None:
                self.items.pop(idx)
                self.reposition()
            self.drag_remove = False
            self.save()
        elif idx is not None and self.hit(e.position()) == idx and not self.items[idx].is_sep:
            self.activate(self.items[idx])
        self.update()

    # ---- ações ----------------------------------------------------------
    def activate(self, item: DockItem) -> None:
        if isinstance(item, SystemItem):
            if item is self.group.start_item:
                self.toggle_start_launcher()
            else:
                self.show_accessibility()
            return
        if item.is_trash:
            open_recycle_bin()
            return
        windows = self.item_windows(item, self.named_windows())
        if windows:
            visible = [w for w in windows if not user32.IsIconic(w)]
            bring_to_front((visible or windows)[0])
        elif not item.transient:
            self.launch(item)

    def toggle_start_launcher(self):
        if self.start_launcher is None:
            self.start_launcher = StartLauncher(self)
        if self.start_launcher.isVisible():
            self.start_launcher.close()
            return
        if not self.isVisible():
            self.group.set_forced("show")
        self.reveal = 1.0
        self.update()
        icon = self.layout()[1][0]
        anchor = self.mapToGlobal(QPoint(round(icon.center().x()), round(icon.top())))
        self.menu_open = True
        self.start_launcher.open_at(anchor)

    def show_accessibility(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(self.pal["menu"])
        for label, command in (
            ("Narrador", "narrator.exe"),
            ("Lupa", "magnify.exe"),
            ("Teclado Virtual", "osk.exe"),
            ("Configurações de acessibilidade", "ms-settings:easeofaccess"),
        ):
            menu.addAction(label, lambda target=command: os.startfile(target))
        anchor = self.layout()[1][self.items.index(self.group.access_item)].toAlignedRect()
        size = menu.sizeHint()
        area = self.screen_ref.geometry()
        point = self.mapToGlobal(anchor.topRight())
        x = max(area.left(), min(point.x() - size.width(), area.right() - size.width() + 1))
        self.menu_open = True
        try:
            menu.exec(QPoint(x, point.y() - size.height() - 7))
        finally:
            self.menu_open = False

    def launch(self, item: DockItem) -> None:
        if not item.running and not item.is_web and not item.is_folder:
            item.learn_t0 = time.monotonic()
            item.learn_before = {h for h, _ in app_windows()}
        try:
            if item.launch_uri:
                os.startfile(item.launch_uri)
            elif item.path.lower().endswith(".exe"):
                os.startfile(item.path, cwd=os.path.dirname(item.path))
            else:
                os.startfile(item.path)
            item.bounce_t0 = time.monotonic()
        except OSError as exc:
            print(f"Não foi possível abrir {item.path}: {exc}", file=sys.stderr)

    def open_with(self, item: DockItem, paths: list[str]) -> None:
        """Abre os arquivos soltos em cima do ícone com o app desse ícone."""
        command = item.open_with_command()
        if not command:
            return
        exe, fixed_args, workdir = command
        files = " ".join(f'"{os.path.normpath(p)}"' for p in paths)
        if not item.running:
            item.learn_t0 = time.monotonic()
            item.learn_before = {h for h, _ in app_windows()}
        try:
            if workdir and os.path.isdir(workdir):
                os.startfile(exe, arguments=f"{fixed_args} {files}".strip(), cwd=workdir)
            else:
                os.startfile(exe, arguments=f"{fixed_args} {files}".strip())
            item.bounce_t0 = time.monotonic()
        except OSError as exc:
            log(f"não foi possível abrir {paths} com {exe}: {exc}")

    def pin_end(self) -> int:
        if not DESKTOP_MODE:
            return len(self.items)
        return next((i for i, item in enumerate(self.items) if item.transient),
                    self.items.index(self.group.end_separator))

    def pin_open_app(self, item):
        if item not in self.items or not item.transient or not item.path.lower().endswith(".exe"):
            return
        key = os.path.normcase(item.target)
        if any(isinstance(other, DockItem) and not other.transient
               and os.path.normcase(other.target) == key for other in self.items):
            return
        index = self.pin_end()
        self.items.remove(item)
        item.transient = False
        self.items.insert(index, item)
        self.save()
        self.reposition()

    @staticmethod
    def pin_start() -> int:
        return 2 if DESKTOP_MODE else 0

    def add_paths(self, paths: list[str], index: int | None = None) -> None:
        end = self.pin_end()
        index = end if index is None else max(self.pin_start(), min(index, end))
        for path in paths:
            path = os.path.normpath(path)
            name = Path(path).stem if Path(path).suffix else Path(path).name
            item = DockItem(name, path, self.provider, self.icon_px)
            if DESKTOP_MODE and self.group.is_explorer(item):
                continue
            self.items.insert(index, item)
            index += 1
        self.save()
        self.reposition()

    def rename_item(self, item: DockItem) -> None:
        self.menu_open = True
        name, ok = QInputDialog.getText(self, "Renomear", "Nome no Dock:", text=clean_name(item.name))
        self.menu_open = False
        if ok and name.strip():
            item.name = name.strip()
            self.save()
            self.reposition()

    def add_trash(self) -> None:
        if self.items and not self.items[-1].is_sep:
            self.items.append(Separator())
        self.items.append(TrashItem(self.icon_px))
        self.save()
        self.reposition()

    def add_separator(self, index: int) -> None:
        index = max(self.pin_start(), min(index, self.pin_end()))
        self.items.insert(index, Separator())
        self.save()
        self.reposition()

    def remove_item(self, item) -> None:
        if item.fixed or item.transient:
            return
        self.items.remove(item)
        self.save()
        self.reposition()

    def save(self) -> None:
        self.group.save()
        self.group.refresh(source=self)  # os outros docks mostram a mudança

    def set_option(self, key: str, value) -> None:
        self.cfg[key] = value
        self.group.save()
        if key == "all_screens":
            self.group.rebuild()  # recria os docks (este inclusive) logo depois do menu fechar
            return
        self.group.reload_icons()  # tamanho dos ícones mudou → recarrega na resolução certa
        for dock in self.group.docks:
            dock.apply_option(key, value)

    def apply_option(self, key: str, value) -> None:
        if key == "blur":
            if value and self.blur is None:
                self.blur = BlurBehind()
            elif not value and self.blur is not None:
                self.blur.close()
                self.blur.deleteLater()
                self.blur = None
        if key == "theme":
            self.theme_name = self.resolve_theme()
        self.reposition()
        self.last_state = None  # força redesenho (e reposicionar o desfoque)

    def pick_apps(self) -> None:
        start_menu = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs"
        self.menu_open = True
        files, _ = QFileDialog.getOpenFileNames(
            self, "Adicionar ao Dock", str(start_menu),
            "Programas e atalhos (*.exe *.lnk *.url);;Todos os arquivos (*.*)",
        )
        self.menu_open = False
        if files:
            self.add_paths(files)
        # A janela de escolher arquivos carrega bastante coisa do Windows; liberar.
        QTimer.singleShot(1500, trim_memory)

    # ---- menu de contexto ----------------------------------------------
    def contextMenuEvent(self, e) -> None:
        self.close_previews()
        menu = self.build_context_menu(e.pos())
        self.menu_open = True
        try:
            menu.exec(e.globalPos())
        finally:
            self.menu_open = False
            menu.deleteLater()

    def build_context_menu(self, pos, idx=None):
        if idx is None:
            idx = self.hit(QPointF(pos))
        style = self.pal["menu"]
        menu = QMenu(self)
        menu.setStyleSheet(style)

        if idx is not None and self.items[idx].is_sep:
            sep = self.items[idx]
            if not sep.fixed:
                menu.addAction("Remover separador", lambda: self.remove_item(sep))
                menu.addSeparator()
        elif idx is not None and self.items[idx].is_trash:
            trash = self.items[idx]
            menu.addAction("Abrir", open_recycle_bin)
            empty = menu.addAction("Esvaziar Lixeira", empty_recycle_bin)
            empty.setEnabled(trash.full)
            if not trash.fixed:
                menu.addAction("Remover do Dock", lambda: self.remove_item(trash))
            menu.addSeparator()
        elif idx is not None and isinstance(self.items[idx], SystemItem):
            control = self.items[idx]
            menu.addAction(control.name, lambda: self.activate(control))
            menu.addSeparator()
        elif idx is not None:
            item = self.items[idx]
            windows = self.item_windows(item, self.named_windows())
            if len(windows) == 1:
                hwnd = windows[0]
                label = "Restaurar janela" if user32.IsIconic(hwnd) else "Voltar à janela"
                menu.addAction(QIcon(item.pixmap), label, lambda: bring_to_front(hwnd))
            elif windows:
                window_menu = menu.addMenu(QIcon(item.pixmap), f"Janelas abertas ({len(windows)})")
                for hwnd in windows:
                    text = window_title(hwnd) or clean_name(item.name)
                    text = text if len(text) <= 60 else text[:57] + "…"
                    if user32.IsIconic(hwnd):
                        text += "  (minimizada)"
                    window_menu.addAction(text.replace("&", "&&"), lambda h=hwnd: bring_to_front(h))
            menu.addAction("Nova janela" if windows else f"Abrir {clean_name(item.name)}".replace("&", "&&"),
                           lambda: self.launch(item))
            if item.transient and item.path.lower().endswith(".exe"):
                menu.addAction("Fixar no Dock", lambda: self.pin_open_app(item))

            shortcut = menu.addMenu("Opções do atalho")
            shortcut.addAction("Mostrar no Explorador",
                               lambda: os.startfile("explorer.exe", arguments=f'/select,"{item.path}"'))
            if not item.transient and not item.fixed:
                shortcut.addAction("Renomear…", lambda: self.rename_item(item))
                shortcut.addAction("Remover do Dock", lambda: self.remove_item(item))
            if windows:
                label = "Fechar janela" if len(windows) == 1 else f"Fechar todas as janelas ({len(windows)})"
                menu.addAction(label, lambda ws=list(windows): [close_window(h) for h in ws])
            menu.addSeparator()

        add = menu if idx is None else menu.addMenu("Adicionar ao Dock")
        add.addAction("Adicionar aplicativo…", self.pick_apps)
        sep_index = idx + 1 if idx is not None else self.insert_index(pos.x())
        add.addAction("Adicionar separador", lambda: self.add_separator(sep_index))
        if not any(it.is_trash for it in self.items):
            add.addAction("Adicionar Lixeira", self.add_trash)

        settings = menu.addMenu("Configurações do Dock")
        size_menu = settings.addMenu("Tamanho dos ícones")
        size_menu.setStyleSheet(style)
        group = QActionGroup(size_menu)
        for label, size in ICON_SIZES.items():
            act = QAction(label, size_menu, checkable=True, checked=self.cfg["icon_size"] == size)
            act.triggered.connect(lambda _=False, s=size: self.set_option("icon_size", s))
            group.addAction(act)
            size_menu.addAction(act)

        magnify = settings.addAction("Ampliação")
        magnify.setCheckable(True)
        magnify.setChecked(self.cfg["magnify"])
        magnify.toggled.connect(lambda on: self.set_option("magnify", on))

        autohide = settings.addAction("Ocultar automaticamente")
        autohide.setCheckable(True)
        autohide.setChecked(self.cfg["autohide"])
        autohide.toggled.connect(lambda on: self.set_option("autohide", on))

        look_menu = settings.addMenu("Aparência")
        look_menu.setStyleSheet(style)
        look_group = QActionGroup(look_menu)
        for label, value in (("Automática (igual ao Windows)", "auto"), ("Escura", "dark"), ("Clara", "light")):
            act = QAction(label, look_menu, checkable=True, checked=self.cfg.get("theme", "auto") == value)
            act.triggered.connect(lambda _=False, v=value: self.set_option("theme", v))
            look_group.addAction(act)
            look_menu.addAction(act)
        look_menu.addSeparator()
        blur = look_menu.addAction("Fundo desfocado")
        blur.setCheckable(True)
        blur.setChecked(self.blur is not None)
        blur.toggled.connect(lambda on: self.set_option("blur", on))
        if self.group.top_bar is not None:
            self.group.top_bar.add_surface_menu(settings, style)

        displays = settings.addMenu("Monitores")
        dock_screens = displays.addMenu("Dock")
        bar_screens = displays.addMenu("Barra superior") if self.group.top_bar is not None else None
        if bar_screens is not None:
            bar_choices = QActionGroup(bar_screens)
            automatic = bar_screens.addAction("Tela principal automática")
            automatic.setCheckable(True)
            automatic.setChecked(not self.cfg.get("top_bar_monitor"))
            bar_choices.addAction(automatic)
            automatic.triggered.connect(lambda: self.group.set_top_bar_monitor(None))
        for number, screen in enumerate(QGuiApplication.screens(), 1):
            label = f"Monitor {number}"
            if screen.model().strip():
                label += f" · {screen.model().strip()}"
            if screen is QGuiApplication.primaryScreen():
                label += " (principal)"
            key = screen_preference_key(screen)
            action = dock_screens.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.group.dock_on_screen(screen))
            action.toggled.connect(lambda enabled, monitor=key: self.group.set_dock_monitor(monitor, enabled))
            if bar_screens is not None:
                target = bar_screens.addAction(label)
                target.setCheckable(True)
                target.setChecked(self.cfg.get("top_bar_monitor") == key)
                bar_choices.addAction(target)
                target.triggered.connect(lambda _checked=False, monitor=key: self.group.set_top_bar_monitor(monitor))

        startup = settings.addAction("Iniciar com o Windows")
        startup.setCheckable(True)
        startup.setEnabled(not (DEV_MODE or TRAY_PREVIEW or SHELL_PROTOTYPE))
        if not (DEV_MODE or TRAY_PREVIEW or SHELL_PROTOTYPE):
            startup.setChecked(startup_enabled())
            startup.toggled.connect(set_startup)

        menu.addSeparator()
        lead = self.group.leader or self  # atalho e atualizações ficam com o dock principal
        focus_text = "Navegar pelo Dock"
        if lead.focus_hotkey_ok:
            focus_text += f"\t{DOCK_FOCUS_SHORTCUT}"
        menu.addAction(focus_text, self.group.focus_dock)
        hide_text = "Ocultar o Dock"
        if lead.hotkey_ok:
            hide_text += f"\t{self.cfg['hotkey']}"
        menu.addAction(hide_text, lambda: self.group.set_forced("hide"))
        help_menu = menu.addMenu("Ajuda e atualizações")
        help_menu.addAction("Como usar o Pingo Dock", self.group.show_welcome)
        if lead.update_info:
            help_menu.addAction(f"Atualizar para a versão {lead.update_info['version']}…", lead.start_update)
        elif update_source(self.cfg):
            help_menu.addAction("Procurar atualizações", lambda: lead.check_updates(manual=True))
        about = help_menu.addAction(f"{APP_TITLE} {APP_VERSION}")
        about.setEnabled(False)
        menu.addAction("Sair do Dock", QApplication.quit)

        return menu

    # ---- arrastar arquivos do Explorador -------------------------------
    def dragEnterEvent(self, e) -> None:
        if any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def drop_target(self, pos: QPointF) -> tuple[int, str] | None:
        """Arquivo em cima (do miolo) de um app → "abrir com"; da Lixeira → apagar.
        Entre ícones (ou nas bordas deles) → fixar no dock, como antes."""
        idx = self.hit(pos)
        if idx is None:
            return None
        item = self.items[idx]
        r = self.layout()[1][idx]
        if not (r.left() + r.width() * 0.12 <= pos.x() <= r.right() - r.width() * 0.12):
            return None
        if item.is_trash:
            return idx, "Mover para a Lixeira"
        if not item.is_sep and item.open_with_command():
            return idx, f"Abrir com {clean_name(item.name)}"
        return None

    def dragMoveEvent(self, e) -> None:
        hint = self.drop_target(e.position())
        if hint != self.drop_hint:
            self.drop_hint = hint
            self.update()
        e.acceptProposedAction()

    def dragLeaveEvent(self, e) -> None:
        self.drop_hint = None
        self.update()

    def dropEvent(self, e) -> None:
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        hint, self.drop_hint = self.drop_target(e.position()), None
        if hint is None:
            self.add_paths(paths, self.insert_index(e.position().x()))
        elif self.items[hint[0]].is_trash:
            send_to_recycle_bin(paths)
        else:
            self.open_with(self.items[hint[0]], paths)
        e.acceptProposedAction()
        self.update()


def selftest() -> int:
    """Confere um build: monta o dock completo por alguns segundos e sai.
    Não migra nada nem mexe no registro. Resultado em selftest.log (pasta de dados)
    e no código de saída (0 = ok). Use com APPDATA apontando para uma pasta de teste."""
    import traceback

    out = DATA_DIR / "selftest.log"
    try:
        app = QApplication(sys.argv[:1])
        app.setStyle("Fusion")
        configure_ui_font(app)
        dock = Dock()
        dock.show()
        QTimer.singleShot(3000, app.quit)
        app.exec()
        items = [it for it in dock.items if not it.is_sep]
        out.write_text(f"ok {APP_TITLE} {APP_VERSION}: {len(items)} itens, "
                       f"lixeira={any(it.is_trash for it in items)}, tema={dock.theme_name}, "
                       f"desfoque={dock.blur is not None and dock.blur.blur_on}, "
                       f"atalho={dock.hotkey_ok}\n", encoding="utf-8")
        return 0
    except Exception:
        out.write_text(traceback.format_exc(), encoding="utf-8")
        return 1


class PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ac", wintypes.BYTE), ("flags", wintypes.BYTE),
        ("percent", wintypes.BYTE), ("saver", wintypes.BYTE),
        ("remaining", wintypes.DWORD), ("full_life", wintypes.DWORD),
    ]


class BatterySymbol(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(28, 16)
        self.percent = 0
        self.charging = False

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        scale_status_symbol(painter, self, 1.08)
        painter.translate(1, 0)
        painter.setPen(QPen(status_ink(self, 190), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(0.75, 3.25, 22, 9.5), 2.7, 2.7)
        painter.setPen(Qt.NoPen)
        painter.setBrush(status_ink(self, 175))
        painter.drawRoundedRect(QRectF(24, 6.1, 1.65, 3.8), 0.75, 0.75)

        fill = 18.5 * self.percent / 100
        if fill > 0:
            painter.setBrush(status_ink(self))
            painter.drawRoundedRect(QRectF(2.5, 5, fill, 6), min(1.3, fill / 2), 1.3)
        if self.charging:
            bolt = QPainterPath(QPointF(12.4, 3.8))
            bolt.lineTo(8.7, 8.5)
            bolt.lineTo(11.3, 8.5)
            bolt.lineTo(10.4, 12.2)
            bolt.lineTo(14.5, 7.1)
            bolt.lineTo(11.7, 7.1)
            bolt.closeSubpath()
            ink = status_ink(self)
            painter.setPen(QPen(ink, 0.55, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.white if ink.lightness() < 128 else QColor("#26292e"))
            painter.drawPath(bolt)


STATUS_BUTTON_STYLE = "QToolButton { background: transparent; border: 0; }"


def status_ink(widget, alpha=255):
    ink = QColor(widget.property("statusInk") or Qt.white)
    ink.setAlpha(alpha)
    return ink


def status_icon_with_shadow(pixmap, strength=1.0):
    size = pixmap.deviceIndependentSize().toSize()
    mask = pixmap.copy()
    painter = QPainter(mask)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(QRectF(0, 0, size.width(), size.height()), Qt.black)
    painter.end()

    shaded = QPixmap((size + QSize(2, 2)) * 2)
    shaded.setDevicePixelRatio(2)
    shaded.fill(Qt.transparent)
    painter = QPainter(shaded)
    painter.setOpacity(0.18 * strength)
    for x, y in ((0, 1), (2, 1), (0.5, 2), (1.5, 2), (1, 2.5)):
        painter.drawPixmap(QPointF(x, y), mask)
    painter.setOpacity(0.42 * strength)
    painter.drawPixmap(QPointF(1, 1.5), mask)
    painter.setOpacity(1)
    painter.drawPixmap(QPointF(1, 0.5), pixmap)
    painter.end()
    return QIcon(shaded)


def scale_status_symbol(painter, widget, scale):
    if widget.property("statusHovered"):
        center = QPointF(widget.width() / 2, widget.height() / 2)
        painter.translate(center)
        painter.scale(scale, scale)
        painter.translate(-center)


class NetworkSymbol(QToolButton):
    def __init__(self):
        super().__init__()
        self.setFixedSize(27, 26)
        self.setAccessibleName("Conexões de rede disponíveis")
        self.setStyleSheet(STATUS_BUTTON_STYLE)
        self.clicked.connect(self.open_connections)
        self.percent = None
        self.connected = False
        self.flyout_timer = QTimer(self, interval=15, timeout=self.anchor_connections)
        self.flyout_deadline = 0.0
        self.previous_foreground = None

    def open_connections(self):
        if SHELL_PROTOTYPE:
            try:
                os.startfile("ms-settings:network-wifi")
            except OSError as error:
                log(f"não foi possível abrir as configurações de rede: {error}")
            return
        self.previous_foreground = user32.GetForegroundWindow()
        try:
            os.startfile("ms-availablenetworks:")
        except OSError as error:
            log(f"não foi possível abrir as conexões disponíveis: {error}")
            return
        self.flyout_deadline = time.monotonic() + 2
        self.flyout_timer.start()

    def anchor_connections(self):
        if time.monotonic() >= self.flyout_deadline:
            self.flyout_timer.stop()
            return
        popup = user32.GetForegroundWindow()
        if (not popup or popup == self.previous_foreground
                or window_class(popup) != "Windows.UI.Core.CoreWindow"
                or process_name(window_pid(popup)) != "shellexperiencehost.exe"):
            return
        top = self.window()
        bar_rect, popup_rect = wintypes.RECT(), wintypes.RECT()
        if not (user32.GetWindowRect(int(top.winId()), ctypes.byref(bar_rect))
                and user32.GetWindowRect(popup, ctypes.byref(popup_rect))):
            return
        self.flyout_timer.stop()
        scale = top.devicePixelRatio()
        anchor = self.mapTo(top, QPoint(0, 0))
        width = popup_rect.right - popup_rect.left
        x = min(bar_rect.left + round(anchor.x() * scale), max(bar_rect.left, bar_rect.right - width))
        y = bar_rect.bottom + round(4 * scale)
        user32.SetWindowPos(popup, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | 0x0004)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        scale_status_symbol(painter, self, 1.12)
        painter.translate(3.5, 5)
        if self.percent is None and self.connected:
            painter.setPen(QPen(status_ink(self), 1.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawRoundedRect(QRectF(3.5, 3.5, 13, 9), 2, 2)
            for x in (8, 10, 12):
                painter.drawLine(QPointF(x, 8.7), QPointF(x, 10))
            return

        signal = self.percent or 0
        for radius, threshold in ((4.6, 20), (8.2, 45), (11.8, 70)):
            color = status_ink(self, 255 if signal >= threshold else 76)
            painter.setPen(QPen(color, 1.55, Qt.SolidLine, Qt.RoundCap))
            painter.drawArc(QRectF(10 - radius, 13.3 - radius, 2 * radius, 2 * radius), 46 * 16, 88 * 16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(status_ink(self, 255 if signal > 0 else 76))
        painter.drawEllipse(QPointF(10, 13.3), 1.25, 1.25)
        if self.percent is None:
            painter.setPen(QPen(status_ink(self), 1.25, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(3.2, 2.9), QPointF(16.8, 13.4))


_proto(ole32.CoCreateInstance, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
       wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))

MMDEVICE_ENUMERATOR = uuid.UUID("bcde0395-e52f-467c-8e3d-c4579291692e").bytes_le
IMMDEVICE_ENUMERATOR = uuid.UUID("a95664d2-9614-4f35-a746-de8db63617e6").bytes_le
IAUDIO_ENDPOINT_VOLUME = uuid.UUID("5cdf2c82-841e-4546-9722-0cf74078229a").bytes_le


def com_method(interface, index, result, *arguments):
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(vtable[index])


def system_volume(new_level=None):
    initialized = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    if initialized < 0 and initialized != -2147417850:  # RPC_E_CHANGED_MODE
        return None
    enumerator, device, endpoint = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        if ole32.CoCreateInstance(MMDEVICE_ENUMERATOR, None, 1, IMMDEVICE_ENUMERATOR,
                                   ctypes.byref(enumerator)) < 0:
            return None
        default_device = com_method(enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_void_p))
        if default_device(enumerator, 0, 1, ctypes.byref(device)) < 0:  # eRender, eMultimedia
            return None
        activate = com_method(device, 3, ctypes.c_long, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        if activate(device, IAUDIO_ENDPOINT_VOLUME, 1, None, ctypes.byref(endpoint)) < 0:
            return None
        scalar, muted = ctypes.c_float(), wintypes.BOOL()
        get_level = com_method(endpoint, 9, ctypes.c_long, ctypes.POINTER(ctypes.c_float))
        get_mute = com_method(endpoint, 15, ctypes.c_long, ctypes.POINTER(wintypes.BOOL))
        if new_level is not None:
            set_level = com_method(endpoint, 7, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)
            if set_level(endpoint, max(0, min(100, new_level)) / 100, None) < 0:
                return None
            if new_level > 0 and get_mute(endpoint, ctypes.byref(muted)) >= 0 and muted.value:
                set_mute = com_method(endpoint, 14, ctypes.c_long, wintypes.BOOL, ctypes.c_void_p)
                set_mute(endpoint, False, None)
        if get_level(endpoint, ctypes.byref(scalar)) < 0 or get_mute(endpoint, ctypes.byref(muted)) < 0:
            return None
        return round(scalar.value * 100), bool(muted.value)
    finally:
        for interface in (endpoint, device, enumerator):
            if interface:
                com_method(interface, 2, wintypes.ULONG)(interface)  # IUnknown::Release
        if initialized >= 0:
            ole32.CoUninitialize()


POPUP_COLORS = {
    "transparent": ("#272a31", 194, "#636a75", "#f4f5f7", "#525864", "#d0d9e7"),
    "black": ("#141414", 255, "#3a3a3a", "#f4f5f7", "#434343", "#dadada"),
    "white": ("#ffffff", 255, "#d5d9e0", "#252a32", "#dde1e7", "#67748a"),
    "glass": ("#343943", 157, "#717784", "#f4f5f7", "#646b79", "#d8e0ed"),
}


class BarPopup(QWidget):
    def __init__(self, parent, flags):
        super().__init__(parent, flags | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.surface = "transparent"
        self.frost = None

    def apply_surface(self, surface):
        self.surface = surface
        self.sync_frost()
        self.update()

    def sync_frost(self):
        if self.surface == "glass" and self.isVisible():
            if self.frost is None:
                self.frost = BlurBehind(radius=10)
            self.frost.place(self.geometry(), int(self.winId()))
        elif self.frost is not None:
            self.frost.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_frost()

    def hideEvent(self, event):
        if self.frost is not None:
            self.frost.close()
            self.frost.deleteLater()
            self.frost = None
        super().hideEvent(event)

    def paintEvent(self, _event):
        background, alpha, border, *_ = POPUP_COLORS[self.surface]
        color = QColor(background)
        color.setAlpha(alpha)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(color)
        painter.setPen(QPen(QColor(border), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
        ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
        ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
        ("exe", wintypes.WCHAR * 260),
    ]


_proto(kernel32.CreateToolhelp32Snapshot, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
_proto(kernel32.Process32FirstW, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
_proto(kernel32.Process32NextW, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
_proto(kernel32.ProcessIdToSessionId, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))

# O executável principal vem primeiro para evitar usar o ícone de um auxiliar.
BACKGROUND_APPS = (
    ("Discord", ("discord.exe", "discordcanary.exe", "discordptb.exe")),
    ("Dropbox", ("dropbox.exe",)),
    ("Epic Games", ("epicgameslauncher.exe",)),
    ("Google Drive", ("googledrivefs.exe",)),
    ("Logitech G HUB", ("lghub.exe",)),
    ("Microsoft Teams", ("ms-teams.exe", "teams.exe")),
    ("NVIDIA", ("nvidia app.exe", "nvidiashare.exe")),
    ("OBS Studio", ("obs64.exe",)),
    ("OneDrive", ("onedrive.exe",)),
    ("PowerToys", ("powertoys.exe",)),
    ("Steam", ("steam.exe",)),
    ("Telegram", ("telegram.exe",)),
    ("TranslucentTB", ("translucenttb.exe",)),
    ("Wallpaper Engine", ("wallpaper64.exe", "wallpaper32.exe", "wallpaperservice32_c.exe")),
    ("WhatsApp", ("whatsapp.root.exe", "whatsapp.exe")),
)


_proto(shell32.SHGetKnownFolderPath, ctypes.c_long, ctypes.c_void_p, wintypes.DWORD,
       wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p))


def tray_executable_path(value, folders):
    value = os.path.expandvars(value.strip().strip('"'))
    match = re.match(r"^(\{[0-9a-f-]{36}\})(?=[\\/])", value, re.IGNORECASE)
    if match:
        key = match[1].casefold()
        if key not in folders:
            guid = ctypes.create_string_buffer(uuid.UUID(key).bytes_le)
            path = ctypes.c_void_p()
            result = shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(path))
            try:
                folders[key] = ctypes.wstring_at(path) if result == 0 and path else ""
            finally:
                if path:
                    ole32.CoTaskMemFree(path)
        if not folders[key]:
            return ""
        value = folders[key] + value[match.end():]
    return os.path.normcase(os.path.normpath(value)) if os.path.isabs(value) else ""


def legacy_registered_background_apps(folders):
    key = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\TrayNotify"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as root:
            stream, kind = winreg.QueryValueEx(root, "IconStreams")
    except OSError:
        return {}
    header_size, record_size, path_size = 20, 1640, 528
    if kind != winreg.REG_BINARY or len(stream) < header_size or (len(stream) - header_size) % record_size:
        return {}
    apps = {}
    for offset in range(header_size, len(stream), record_size):
        value = stream[offset:offset + path_size].decode("utf-16-le", errors="replace").split("\0", 1)[0]
        try:
            path = tray_executable_path(codecs.decode(value, "rot_13"), folders)
        except ValueError:
            continue
        if path.endswith(".exe"):
            apps.setdefault(path, Path(path).stem)
    return apps


def registered_background_apps():
    """Lê as preferências de ícones do perfil, sem consultar a interface do Explorer."""
    registrations = {}
    folders = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\NotifyIconSettings") as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                        value, _ = winreg.QueryValueEx(key, "ExecutablePath")
                        if not isinstance(value, str):
                            continue
                        path = tray_executable_path(value, folders)
                        if not path or not path.endswith(".exe"):
                            continue
                        try:
                            label, _ = winreg.QueryValueEx(key, "InitialTooltip")
                        except OSError:
                            label = ""
                        if isinstance(label, str):
                            label = next(iter(label.strip().splitlines()), "")[:80]
                        else:
                            label = ""
                        if label and not label.startswith("@"):
                            registrations.setdefault(path, label)
                        else:
                            registrations.setdefault(path, Path(path).stem)
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    for path, label in legacy_registered_background_apps(folders).items():
        registrations.setdefault(path, label)
    return registrations


def background_apps():
    """Consulta uma vez, ao abrir o painel, os apps reconhecidos nesta sessão."""
    current = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(current)):
        return []
    snapshot = kernel32.CreateToolhelp32Snapshot(0x02, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        return []
    registrations = registered_background_apps()
    names = {exe for _, executables in BACKGROUND_APPS for exe in executables}
    names.update(os.path.basename(path) for path in registrations)
    # Vários ícones do sistema usam estes processos como hospedeiros.
    names.difference_update({"explorer.exe", "sihost.exe", "svchost.exe", "systemsettings.exe"})
    processes = {}
    entry = ProcessEntry(size=ctypes.sizeof(ProcessEntry))
    try:
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            name = entry.exe.casefold()
            if name in names:
                session = wintypes.DWORD()
                if kernel32.ProcessIdToSessionId(entry.pid, ctypes.byref(session)) and session.value == current.value:
                    processes.setdefault(name, []).append(entry.pid)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    apps = []
    known_pids = set()
    for label, executables in BACKGROUND_APPS:
        pids = [pid for name in executables for pid in processes.get(name, ())]
        if pids:
            path = next((path for pid in pids if (path := process_image_path(pid))), "")
            apps.append((label, pids, path))
            known_pids.update(pids)

    registered = {}
    for pids in processes.values():
        for pid in pids:
            if pid in known_pids:
                continue
            path = process_image_path(pid)
            key = os.path.normcase(os.path.normpath(path)) if path else ""
            if key in registrations:
                registered.setdefault(key, (registrations[key], [], path))[1].append(pid)
    return sorted(apps + list(registered.values()), key=lambda app: app[0].casefold())


class BackgroundTray(BarPopup):
    def __init__(self, bar):
        super().__init__(bar, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("Aplicativos em segundo plano — Pingo Dock")
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(7, 7, 7, 7)
        self.grid.setSpacing(2)
        self.provider = QFileIconProvider()
        self.action_menu = None
        self.host = None
        self.icon_buttons = []
        self.drawn_revision = -1
        self.refresh_timer = QTimer(self, interval=200, timeout=self.refresh_icons)
        self.last_app_pid = None
        self.mouse_was_down = False
        self.dismiss_timer = QTimer(self, interval=30, timeout=self.dismiss_on_outside_click)

    def apply_surface(self, surface):
        super().apply_surface(surface)
        foreground = POPUP_COLORS[surface][3]
        self.setStyleSheet(f"QLabel {{ color: {foreground}; font-size: 11px; }}")

    def open(self, anchor):
        self.render_icons(anchor)
        self.show()
        self.raise_()
        self.sync_icon_locations()
        self.mouse_was_down = self.mouse_is_down()
        self.dismiss_timer.start()
        if self.host and self.host.hwnd:
            self.refresh_timer.start()

    def refresh_icons(self):
        if (self.host.revision != self.drawn_revision and not self.mouse_is_down()
                and self.action_menu is None):
            self.render_icons(self.parentWidget().tray)
            self.sync_icon_locations()
            self.sync_frost()

    def render_icons(self, anchor):
        self.clear_icons()
        has_receiver = bool(self.host and self.host.hwnd)
        icons = sorted(self.host.items(), key=lambda icon: icon.label.casefold()) if has_receiver else []
        self.drawn_revision = self.host.revision if has_receiver else -1
        apps = [] if has_receiver else background_apps()
        windows = {}
        if apps:
            for hwnd, pid in app_windows():
                windows.setdefault(pid, []).append(hwnd)
        sections = {"Sistema": [], "Aplicativos": []}
        shell_pid = window_pid(self.host.native) if has_receiver and self.host.native else 0
        paths = {}
        for icon in icons:
            if icon.pid not in paths:
                paths[icon.pid] = process_image_path(icon.pid)
            path = paths[icon.pid]
            label = icon.label or Path(path).stem or "Ícone de notificação"
            button = QToolButton()
            button.setFixedSize(30, 30)
            button.setIconSize(QSize(16, 16))
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.setStyleSheet(STATUS_BUTTON_STYLE)
            if not icon.image.isNull():
                button.setIcon(self.normalized_icon(icon.image, anchor.devicePixelRatioF()))
            else:
                button.setText(label[:1])
            button.setContextMenuPolicy(Qt.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda _point, item=icon: self.activate_notification_icon(item, True))
            button.clicked.connect(lambda _checked=False, item=icon: self.activate_notification_icon(item, False))
            sections[self.icon_section(path, icon.pid == shell_pid)].append(button)
            self.icon_buttons.append((icon, button))
        if not apps and not icons:
            message = ("Aguardando o registro de ícones dos aplicativos." if has_receiver
                       else "Nenhum aplicativo compatível em execução.")
            self.grid.addWidget(QLabel(message), 0, 0, 1, 5)
        for label, pids, path in apps:
            button = QToolButton()
            button.setFixedSize(30, 30)
            button.setIconSize(QSize(16, 16))
            button.setAccessibleName(label)
            button.setStyleSheet(STATUS_BUTTON_STYLE)
            app_handles = tuple(hwnd for pid in pids for hwnd in windows.get(pid, ()))
            launch_uri = packaged_launch_uri(pids[0])
            button.setToolTip(f"{label} · Clique com o botão direito para ver as ações")
            if path:
                pixmap, _ = load_icon(path, path, self.provider, 48,
                                      launch_uri=launch_uri)
                button.setIcon(QIcon(pixmap))
            else:
                button.setText(label[:1])
            button.setContextMenuPolicy(Qt.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda point, control=button, name=label, exe=path, uri=launch_uri, handles=app_handles:
                self.show_app_actions(control.mapToGlobal(point), name, exe, uri, handles))
            if app_handles:
                button.clicked.connect(lambda _=False, window=app_handles[0]: self.select(window))
            elif launch_uri:
                button.clicked.connect(lambda _=False, uri=launch_uri: self.open_target(uri))
            sections[self.icon_section(path)].append(button)

        row = 0
        for name, buttons in sections.items():
            if not buttons:
                continue
            heading = QLabel(name)
            heading.setContentsMargins(4, 3 if row else 0, 4, 2)
            self.grid.addWidget(heading, row, 0, 1, 5)
            row += 1
            for index, button in enumerate(buttons):
                self.grid.addWidget(button, row + index // 5, index % 5)
            row += math.ceil(len(buttons) / 5)

        self.adjustSize()
        area = anchor.screen().availableGeometry()
        top = anchor.mapToGlobal(QPoint(anchor.width() - self.width(), anchor.height() + 4))
        self.move(min(max(area.left(), top.x()), area.right() - self.width() + 1), top.y())
    @staticmethod
    def icon_section(path, owned_by_shell=False):
        windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
        file = Path(path)
        system_owners = {"explorer.exe", "securityhealthsystray.exe", "sihost.exe", "systray.exe"}
        if owned_by_shell or (file.is_relative_to(windows) and file.name.casefold() in system_owners):
            return "Sistema"
        return "Aplicativos"

    @staticmethod
    def normalized_icon(source, scale):
        source = source.convertToFormat(QImage.Format_ARGB32)
        visible = [(x, y) for y in range(source.height()) for x in range(source.width())
                   if source.pixelColor(x, y).alpha() > 20]
        if visible:
            left = min(x for x, _ in visible)
            top = min(y for _, y in visible)
            source = source.copy(QRect(QPoint(left, top), QPoint(max(x for x, _ in visible),
                                                               max(y for _, y in visible))))
        size = round(16 * scale)
        limit = min(round(14 * scale), max(source.width(), source.height()))
        if max(source.width(), source.height()) > limit:
            source = source.scaled(limit, limit, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        canvas = QPixmap(size, size)
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        painter.drawImage((size - source.width()) // 2, (size - source.height()) // 2, source)
        painter.end()
        canvas.setDevicePixelRatio(scale)
        return QIcon(canvas)

    def sync_icon_locations(self):
        frame = wintypes.RECT()
        if not user32.GetWindowRect(int(self.winId()), ctypes.byref(frame)):
            return
        scale = self.devicePixelRatioF()
        for icon, button in self.icon_buttons:
            bounds = button.geometry()
            icon.bounds = (frame.left + round(bounds.left() * scale), frame.top + round(bounds.top() * scale),
                           frame.left + round((bounds.right() + 1) * scale),
                           frame.top + round((bounds.bottom() + 1) * scale))

    @staticmethod
    def mouse_is_down():
        return any(user32.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4))

    def dismiss_on_outside_click(self):
        down = self.mouse_is_down()
        clicked = down and not self.mouse_was_down
        self.mouse_was_down = down
        if not clicked or self.action_menu or self.geometry().contains(QCursor.pos()):
            return
        bar = self.parentWidget()
        if bar.tray.rect().contains(bar.tray.mapFromGlobal(QCursor.pos())):
            return
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        native = user32.WindowFromPoint(point)
        if self.last_app_pid and native and window_pid(native) == self.last_app_pid:
            return
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def activate_notification_icon(self, icon, right):
        self.last_app_pid = icon.pid
        self.sync_icon_locations()
        if not icon.click(right):
            log("não foi possível encaminhar o clique ao ícone de notificação")

    def show_app_actions(self, point, label, path, launch_uri, windows):
        if self.action_menu is not None:
            self.action_menu.close()
        menu = QMenu(self)
        self.action_menu = menu
        menu.setStyleSheet(MENU_STYLE_LIGHT if self.surface == "white" else MENU_STYLE)
        menu.addAction(label.replace("&", "&&")).setEnabled(False)
        menu.addSeparator()
        for hwnd in windows:
            title = window_title(hwnd) or label
            menu.addAction(f"Abrir janela · {title[:72]}".replace("&", "&&"),
                           lambda window=hwnd: self.select(window))
        if not windows and launch_uri:
            menu.addAction("Abrir aplicativo", lambda: self.open_target(launch_uri))
        if label == "Wallpaper Engine" and Path(path).name.casefold() in ("wallpaper32.exe", "wallpaper64.exe"):
            for text, command in (("Pausar papel de parede", "pause"), ("Retomar papel de parede", "play"),
                                  ("Próximo papel de parede", "nextWallpaper")):
                menu.addAction(text, lambda value=command: self.open_target(path, f"-control {value}"))
        if path:
            menu.addAction("Mostrar no Explorador",
                           lambda: self.open_target("explorer.exe", f'/select,"{path}"'))
        menu.addAction("Abrir Gerenciador de Tarefas", lambda: self.open_target("taskmgr.exe"))
        try:
            menu.exec(point)
        finally:
            self.action_menu = None
            menu.deleteLater()

    def open_target(self, target, arguments=""):
        self.close()
        try:
            os.startfile(target, arguments=arguments)
        except OSError as error:
            log(f"não foi possível abrir a ação da bandeja para {target}: {error}")

    def select(self, hwnd):
        self.close()
        bring_to_front(hwnd)

    def clear_icons(self):
        for icon, _button in self.icon_buttons:
            icon.bounds = None
        self.icon_buttons.clear()
        while self.grid.count():
            widget = self.grid.takeAt(self.grid.count() - 1).widget()
            widget.hide()
            widget.deleteLater()

    def hideEvent(self, event):
        self.dismiss_timer.stop()
        self.refresh_timer.stop()
        self.last_app_pid = None
        self.clear_icons()
        super().hideEvent(event)


class VolumePopup(BarPopup):
    def __init__(self, button):
        super().__init__(button, Qt.Popup)
        self.setWindowTitle("Volume — Pingo Dock")
        self.setFixedSize(56, 173)
        self.label = QLabel(alignment=Qt.AlignCenter)
        self.slider = QSlider(Qt.Vertical)
        self.slider.setRange(0, 100)
        self.slider.setAccessibleName("Volume do sistema")
        self.slider.valueChanged.connect(button.set_level)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 11, 8, 9)
        layout.setSpacing(6)
        layout.addWidget(self.slider, 1, Qt.AlignHCenter)
        layout.addWidget(self.label)
        self.apply_surface("transparent")

    def apply_surface(self, surface):
        super().apply_surface(surface)
        _, _, _, foreground, rail, fill = POPUP_COLORS[surface]
        self.setStyleSheet(f"""
            QLabel {{ color: {foreground}; font-size: 9pt; }}
            QSlider::groove:vertical {{ background: {rail}; width: 5px; border-radius: 2px; }}
            QSlider::sub-page:vertical {{ background: {rail}; border-radius: 2px; }}
            QSlider::add-page:vertical {{ background: {fill}; border-radius: 2px; }}
            QSlider::handle:vertical {{ background: {foreground}; height: 13px; margin: 0 -4px; border-radius: 6px; }}
        """)

    def display(self, state):
        self.slider.setEnabled(state is not None)
        if state is None:
            self.label.setText("—")
            return
        level, muted = state
        with QSignalBlocker(self.slider):
            if not self.slider.isSliderDown():
                self.slider.setValue(level)
        self.label.setText(f"{level}%")
        self.label.setToolTip("Mudo" if muted else "")

class VolumeButton(QToolButton):
    def __init__(self):
        super().__init__()
        self.state = None
        self.setFixedSize(27, 26)
        self.setIconSize(QSize(23, 20))
        self.setAccessibleName("Volume")
        self.setStyleSheet(STATUS_BUTTON_STYLE)
        self.popup = VolumePopup(self)
        self.clicked.connect(self.toggle_popup)

    def refresh(self):
        self.display_state(system_volume())

    def display_state(self, state):
        if self.popup.isVisible():
            self.popup.display(state)
        if state == self.state and self.toolTip():
            return
        self.state = state
        if state is None:
            text = "Volume indisponível"
        else:
            level, muted = state
            text = f"Volume: {level}%" + (" (mudo)" if muted else "")
        self.setToolTip(text)
        self.setIcon(self.volume_icon())

    def set_level(self, level):
        self.display_state(system_volume(level))

    def toggle_popup(self):
        if self.popup.isVisible():
            self.popup.close()
            return
        anchor = self.mapToGlobal(QPoint(self.width() // 2, self.height()))
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        area = screen.geometry()
        x = min(max(area.left(), anchor.x() - self.popup.width() // 2),
                area.right() - self.popup.width() + 1)
        self.popup.move(x, anchor.y() + 7)
        self.popup.display(self.state)
        self.popup.show()
        self.popup.slider.setFocus()

    def volume_icon(self):
        size = QSize(21, 18)
        pixmap = QPixmap(size * 2)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.translate(0.5, 1)
        speaker = QPainterPath(QPointF(2.4, 5.3))
        speaker.lineTo(4.9, 5.3)
        speaker.lineTo(8.6, 2.1)
        speaker.quadTo(9.6, 1.3, 9.6, 2.6)
        speaker.lineTo(9.6, 13.4)
        speaker.quadTo(9.6, 14.7, 8.6, 13.9)
        speaker.lineTo(4.9, 10.7)
        speaker.lineTo(2.4, 10.7)
        speaker.quadTo(1.3, 10.7, 1.3, 9.6)
        speaker.lineTo(1.3, 6.4)
        speaker.quadTo(1.3, 5.3, 2.4, 5.3)
        speaker.closeSubpath()
        painter.setPen(QPen(status_ink(self), 1.35, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(speaker)
        if self.state is None or self.state[1]:
            painter.setPen(QPen(status_ink(self), 1.45, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(13.2, 6), QPointF(17.2, 10))
            painter.drawLine(QPointF(17.2, 6), QPointF(13.2, 10))
        else:
            level = self.state[0]
            for radius, threshold in ((3.8, 1), (6.9, 34), (10.0, 67)):
                color = status_ink(self, 255 if level >= threshold else 76)
                painter.setPen(QPen(color, 1.55, Qt.SolidLine, Qt.RoundCap))
                painter.drawArc(QRectF(9.1 - radius, 8 - radius, 2 * radius, 2 * radius),
                                -37 * 16, 74 * 16)
        painter.end()
        strength = 0.2 if status_ink(self).lightness() < 128 else 1.0
        return status_icon_with_shadow(pixmap, strength)


class BluetoothButton(QToolButton):
    def __init__(self):
        super().__init__()
        self.setAccessibleName("Bluetooth")
        self.setToolTip("Abrir configurações de Bluetooth")
        self.setStyleSheet(STATUS_BUTTON_STYLE)
        self.setFixedSize(27, 26)
        self.setIconSize(QSize(18, 20))
        self.clicked.connect(self.open_settings)

    def refresh_icon(self):
        pixmap = QPixmap(32, 36)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(status_ink(self), 1.45, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        rune = QPainterPath(QPointF(4, 5))
        rune.lineTo(12, 12.5)
        rune.lineTo(8, 16)
        rune.lineTo(8, 2)
        rune.lineTo(12, 5.5)
        rune.lineTo(4, 13)
        painter.drawPath(rune)
        painter.end()
        strength = 0.2 if status_ink(self).lightness() < 128 else 1.0
        self.setIcon(status_icon_with_shadow(pixmap, strength))

    @staticmethod
    def open_settings():
        try:
            os.startfile("ms-settings:bluetooth")
        except OSError as error:
            log(f"não foi possível abrir as configurações de Bluetooth: {error}")


class DesktopButton(QToolButton):
    def __init__(self):
        super().__init__()
        self.setAccessibleName("Mostrar Área de Trabalho")
        self.setToolTip("Mostrar Área de Trabalho (Win+D)")
        self.setStyleSheet(STATUS_BUTTON_STYLE)
        self.setFixedSize(29, 26)
        self.setIconSize(QSize(22, 20))
        self.minimized_windows = []
        self.clicked.connect(self.toggle_desktop)

    def refresh_icon(self):
        pixmap = QPixmap(40, 36)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        outline = QRectF(2.0, 3.25, 16, 11.5)
        painter.setPen(QPen(status_ink(self), 1.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawRoundedRect(outline, 2.2, 2.2)
        painter.drawLine(QPointF(7, 12), QPointF(13, 12))
        painter.end()
        self.setIcon(status_icon_with_shadow(pixmap))

    def toggle_desktop(self):
        if SHELL_PROTOTYPE:
            if self.minimized_windows:
                for hwnd, pid in self.minimized_windows:
                    if window_pid(hwnd) == pid and user32.IsIconic(hwnd):
                        user32.ShowWindowAsync(hwnd, SW_RESTORE)
                self.minimized_windows.clear()
            else:
                self.minimized_windows = [
                    (hwnd, pid) for hwnd, pid in app_windows()
                    if pid != os.getpid() and not user32.IsIconic(hwnd)
                    and user32.ShowWindowAsync(hwnd, 6)  # SW_MINIMIZE
                ]
            return
        windows, d = 0x5B, 0x44  # VK_LWIN, D
        user32.keybd_event(windows, 0, 0, 0)
        user32.keybd_event(d, 0, 0, 0)
        user32.keybd_event(d, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(windows, 0, KEYEVENTF_KEYUP, 0)


TOP_BAR_SURFACES = (
    ("transparent", "Transparente"),
    ("black", "Preto"),
    ("white", "Branco"),
    ("glass", "Vidro fosco"),
)


class AppBarData(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("window", HWND), ("callback", wintypes.UINT),
                ("edge", wintypes.UINT), ("rect", wintypes.RECT), ("param", wintypes.LPARAM)]


APPBAR_CALLBACK = 0x8124
ABM_NEW, ABM_REMOVE, ABM_QUERYPOS, ABM_SETPOS = range(4)
ABE_TOP = 1
ABN_POSCHANGED = 1
_proto(shell32.SHAppBarMessage, ctypes.c_size_t, wintypes.DWORD, ctypes.POINTER(AppBarData))
_proto(user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)

class TopBar(QWidget):
    def __init__(self, group=None):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setWindowTitle(f"{APP_TITLE} — Barra superior")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.locale = QLocale("pt_BR")
        self.group = group
        self.work_area_enabled = False
        self.appbar_registered = False
        self.appbar_rect = None
        self.appbar_position = None
        self.native_taskbar_visible = None
        self.appbar_native_visible = None
        self.work_area_deadline = 0.0
        self.explorer_restart = user32.RegisterWindowMessageW("TaskbarCreated")
        self.appbar_timer = QTimer(self, singleShot=True, interval=60, timeout=self.reserve_work_area)
        self.fullscreen_timer = QTimer(self, interval=250, timeout=self.follow_fullscreen)
        selected = group.cfg.get("top_bar_style") if group is not None else None
        self.surface = selected if selected in dict(TOP_BAR_SURFACES) else "transparent"
        self.surface_blur = None

        layout = QGridLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(2, 1)
        indicators = QHBoxLayout()
        indicators.setSpacing(7)
        self.battery = QWidget()
        battery_layout = QHBoxLayout(self.battery)
        battery_layout.setContentsMargins(0, 0, 0, 0)
        battery_layout.setSpacing(5)
        self.battery_symbol = BatterySymbol()
        self.battery_percent = QLabel()
        battery_layout.addWidget(self.battery_symbol)
        battery_layout.addWidget(self.battery_percent)
        self.network = QWidget()
        network_policy = self.network.sizePolicy()
        network_policy.setRetainSizeWhenHidden(True)
        self.network.setSizePolicy(network_policy)
        self.network.hide()
        network_layout = QHBoxLayout(self.network)
        network_layout.setContentsMargins(0, 0, 0, 0)
        network_layout.setSpacing(5)
        self.network_symbol = NetworkSymbol()
        self.network_text = QLabel()
        network_layout.addWidget(self.network_symbol)
        network_layout.addWidget(self.network_text)
        self.volume = VolumeButton()
        self.bluetooth = BluetoothButton()
        for indicator in (self.battery, self.network):
            self.add_shadow(indicator)
            indicators.addWidget(indicator)
        indicators.addWidget(self.volume)
        indicators.addWidget(self.bluetooth)
        indicators.addStretch()
        layout.addLayout(indicators, 0, 0)
        self.clock = QLabel()
        self.add_shadow(self.clock)
        layout.addWidget(self.clock, 0, 1, Qt.AlignCenter)
        self.tray = QToolButton()
        self.tray.setAccessibleName("Ícones em segundo plano")
        self.tray.setToolTip("Ícones em segundo plano")
        self.tray.setStyleSheet(STATUS_BUTTON_STYLE)
        self.tray.setFixedSize(30, 26)
        self.tray.setIconSize(QSize(18, 18))
        self.tray.clicked.connect(self.open_tray)
        self.desktop = DesktopButton()
        right = QHBoxLayout()
        right.setSpacing(2)
        right.addStretch()
        right.addWidget(self.tray)
        right.addWidget(self.desktop)
        layout.addLayout(right, 0, 2)
        self.status_icons = (self.battery_symbol, self.network_symbol, self.volume, self.bluetooth,
                             self.tray, self.desktop)
        for icon in self.status_icons:
            if icon in (self.volume, self.bluetooth, self.tray, self.desktop):
                icon.setProperty("statusIconSize", icon.iconSize())
            icon.installEventFilter(self)
        self.background_tray = BackgroundTray(self)
        QApplication.instance().aboutToQuit.connect(self.reset_tray_session)
        self.context_timer = QTimer(self, interval=8, timerType=Qt.PreciseTimer,
                                    timeout=self.dismiss_native_menu)
        self.context_owner = None
        self.context_opened = False
        self.context_deadline = 0.0

        self.wifi = QProcess(self)
        self.wifi.finished.connect(self.refresh_network)
        self.wifi.errorOccurred.connect(self.refresh_network)
        self.clock_timer = QTimer(self, interval=1000, timeout=self.refresh_clock)
        self.volume_timer = QTimer(self, interval=1000, timeout=self.volume.refresh)
        self.status_timer = QTimer(self, interval=8000, timeout=self.refresh_status)
        self.clock_timer.start()
        self.volume_timer.start()
        self.status_timer.start()
        self.screen = None
        if self.group is None:
            QGuiApplication.instance().primaryScreenChanged.connect(self.follow_screen)
        self.follow_screen(self.group.top_bar_screen() if self.group is not None
                           else QGuiApplication.primaryScreen())
        self.refresh_surface_colors()
        self.refresh_clock()
        self.volume.refresh()
        self.refresh_status()

    @staticmethod
    def add_shadow(widget):
        shadow = QGraphicsDropShadowEffect(widget)
        shadow.setBlurRadius(5)
        shadow.setOffset(0, 1)
        shadow.setColor(QColor(0, 0, 0, 230))
        widget.setGraphicsEffect(shadow)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Enter, QEvent.Leave):
            hovered = event.type() == QEvent.Enter
            watched.setProperty("statusHovered", hovered)
            size = watched.property("statusIconSize")
            if size is not None:
                watched.setIconSize(size + QSize(2, 2) if hovered else size)
            else:
                watched.update()
        return super().eventFilter(watched, event)

    def follow_screen(self, screen):
        changed_screen = self.screen is not None and self.screen is not screen
        if self.screen is not None:
            try:
                self.screen.geometryChanged.disconnect(self.on_screen_geometry_changed)
            except RuntimeError:
                pass
        self.screen = screen
        if screen is not None:
            if changed_screen:
                self.reset_tray_session()
                self.volume.popup.close()
                self.unregister_appbar()
            screen.geometryChanged.connect(self.on_screen_geometry_changed)
            self.place(screen.geometry())

    def on_screen_geometry_changed(self, geometry):
        self.reset_tray_session()
        self.place(geometry)

    def place(self, geometry):
        if not self.appbar_registered:
            self.setGeometry(geometry.x(), geometry.y(), geometry.width(), 32)
        if self.work_area_enabled:
            self.appbar_timer.start()
        self.sync_surface()

    def enable_work_area(self):
        self.work_area_enabled = True
        self.work_area_deadline = time.monotonic() + 2
        QApplication.instance().aboutToQuit.connect(self.release_work_area)
        self.appbar_timer.start(350)
        self.fullscreen_timer.start()

    def appbar_data(self):
        return AppBarData(ctypes.sizeof(AppBarData), int(self.winId()), APPBAR_CALLBACK, ABE_TOP)

    def reserve_work_area(self):
        native = explorer_taskbar()
        native_visible = bool(native and user32.IsWindowVisible(native))
        if not self.appbar_registered and native_visible and time.monotonic() < self.work_area_deadline:
            self.appbar_timer.start(60)
            return
        data = self.appbar_data()
        if not self.appbar_registered:
            self.appbar_registered = bool(shell32.SHAppBarMessage(ABM_NEW, ctypes.byref(data)))
            if not self.appbar_registered:
                return
        monitor = user32.MonitorFromWindow(data.window, MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return
        height = round(32 * self.devicePixelRatio())
        bounds = info.rcMonitor
        data.rect = wintypes.RECT(bounds.left, bounds.top, bounds.right, bounds.top + height)
        shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(data))
        visual_top = data.rect.top
        # O Explorer pode manter a reserva da barra nativa mesmo após ocultá-la.
        native_rect = wintypes.RECT()
        if (native and not native_visible
                and user32.GetWindowRect(native, ctypes.byref(native_rect))
                and native_rect.top == bounds.top and native_rect.left <= bounds.left
                and native_rect.right >= bounds.right and data.rect.top <= native_rect.bottom):
            visual_top = bounds.top
        data.rect.bottom = max(data.rect.top + 1, visual_top + height)
        requested = (data.rect.left, data.rect.top, data.rect.right, data.rect.bottom, visual_top)
        if requested == self.appbar_position:
            return
        if not shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(data)):
            return
        r = data.rect
        self.appbar_position = requested
        self.appbar_native_visible = native_visible
        self.appbar_rect = (r.left, visual_top, r.right, visual_top + height)
        user32.SetWindowPos(data.window, None, r.left, visual_top, r.right - r.left, height,
                            SWP_NOACTIVATE | 0x0004)
        self.sync_surface()
        QTimer.singleShot(150, self.fit_overlapping_windows)

    def fit_overlapping_windows(self):
        if self.appbar_rect is None or not self.isVisible():
            return
        native = explorer_taskbar()
        if bool(native and user32.IsWindowVisible(native)) != self.appbar_native_visible:
            return
        _, top, _, bottom = self.appbar_rect
        monitor = user32.MonitorFromWindow(int(self.winId()), MONITOR_DEFAULTTONEAREST)
        for hwnd, pid in app_windows():
            if (pid == os.getpid() or user32.IsIconic(hwnd) or user32.IsZoomed(hwnd)
                    or user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST) != monitor
                    or user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION != WS_CAPTION):
                continue
            outer, frame = wintypes.RECT(), wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(outer)):
                continue
            if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame)) != 0:
                frame = outer
            if frame.top < bottom and frame.bottom > top:
                user32.SetWindowPos(hwnd, None, outer.left, outer.top + bottom - frame.top, 0, 0,
                                    SWP_NOSIZE | SWP_NOACTIVATE | 0x0004)

    def release_work_area(self):
        self.appbar_timer.stop()
        self.fullscreen_timer.stop()
        self.work_area_enabled = False
        self.unregister_appbar()

    def unregister_appbar(self):
        if self.appbar_registered:
            data = self.appbar_data()
            shell32.SHAppBarMessage(ABM_REMOVE, ctypes.byref(data))
            self.appbar_registered = False
            self.appbar_rect = None
            self.appbar_position = None

    def nativeEvent(self, event_type, message):
        if hasattr(self, "background_tray") and bytes(event_type.data()) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if self.explorer_restart and msg.message == self.explorer_restart:
                host = self.background_tray.host
                if host:
                    host.ensure_receiver()
                if self.work_area_enabled:
                    self.appbar_registered = False
                    self.appbar_rect = None
                    self.appbar_position = None
                    self.appbar_timer.start()
            elif msg.message == 0x0218 and msg.wParam in (0x07, 0x12):  # WM_POWERBROADCAST / resume
                host = self.background_tray.host
                if host and host.hwnd and not host.recovery.isActive():
                    host.recovery.start()
                if self.work_area_enabled:
                    self.appbar_timer.start()
            elif self.work_area_enabled and msg.message == APPBAR_CALLBACK and msg.wParam == ABN_POSCHANGED:
                self.appbar_timer.start()
        return super().nativeEvent(event_type, message)

    def follow_fullscreen(self):
        native = explorer_taskbar()
        native_visible = bool(native and user32.IsWindowVisible(native))
        if native_visible != self.native_taskbar_visible:
            self.native_taskbar_visible = native_visible
            self.appbar_timer.start(60)
        hwnd = user32.GetForegroundWindow()
        if not hwnd or window_pid(hwnd) == os.getpid():
            return
        monitor = user32.MonitorFromWindow(int(self.winId()), MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        rect = wintypes.RECT()
        fullscreen = (
            user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST) == monitor
            and not user32.IsZoomed(hwnd) and is_app_window(hwnd)
            and not user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_LAYERED
            and user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION != WS_CAPTION
            and user32.GetMonitorInfoW(monitor, ctypes.byref(info))
            and user32.GetWindowRect(hwnd, ctypes.byref(rect)) and _covers(rect, info.rcMonitor, tol=2)
        )
        if fullscreen and self.isVisible():
            self.volume.popup.close()
            self.reset_tray_session()
            self.hide()
        elif not fullscreen and not self.isVisible():
            self.show()

    def add_surface_menu(self, menu, style):
        surface_menu = menu.addMenu("Aparência da barra superior")
        surface_menu.setStyleSheet(style)
        choices = QActionGroup(surface_menu)
        for value, label in TOP_BAR_SURFACES:
            action = surface_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.surface == value)
            choices.addAction(action)
            action.triggered.connect(lambda _checked=False, mode=value: self.set_surface(mode))
        return surface_menu

    def set_surface(self, mode):
        if mode == self.surface or mode not in dict(TOP_BAR_SURFACES):
            return
        self.surface = mode
        self.refresh_surface_colors()
        self.sync_surface()
        self.update()
        if self.group is not None:
            self.group.cfg["top_bar_style"] = mode
            self.group.save()

    def refresh_surface_colors(self):
        light = self.surface == "white"
        ink = QColor("#252a32" if light else "#ffffff")
        self.setStyleSheet(f"QLabel, QToolButton {{ color: {ink.name()}; font-size: 10pt; }}")
        self.volume.popup.apply_surface(self.surface)
        self.background_tray.apply_surface(self.surface)
        for widget in self.status_icons:
            widget.setProperty("statusInk", ink)
            widget.update()
        for widget in (self.battery, self.network, self.clock):
            widget.graphicsEffect().setColor(QColor(0, 0, 0, 45 if light else 230))
        self.volume.setIcon(self.volume.volume_icon())
        self.bluetooth.refresh_icon()
        self.desktop.refresh_icon()

        chevron = QPixmap(32, 32)
        chevron.setDevicePixelRatio(2)
        chevron.fill(Qt.transparent)
        painter = QPainter(chevron)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath(QPointF(3.5, 6))
        path.lineTo(8, 10.5)
        path.lineTo(12.5, 6)
        painter.setPen(QPen(ink, 1.15, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(path)
        painter.end()
        self.tray.setIcon(status_icon_with_shadow(chevron))

    def sync_surface(self):
        if self.surface == "glass" and self.isVisible():
            if self.surface_blur is None:
                self.surface_blur = BlurBehind(radius=0)
            self.surface_blur.place(self.geometry(), int(self.winId()))
        elif self.surface_blur is not None:
            self.surface_blur.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_surface()

    def hideEvent(self, event):
        if self.surface_blur is not None:
            self.surface_blur.hide()
        super().hideEvent(event)

    def closeEvent(self, event):
        if self.work_area_enabled:
            self.release_work_area()
        super().closeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        if self.surface == "black":
            painter.fillRect(event.rect(), Qt.black)
        elif self.surface == "white":
            painter.fillRect(event.rect(), Qt.white)
        elif self.surface == "glass":
            palette = THEMES["dark"]
            blurred = self.surface_blur is not None and self.surface_blur.blur_on
            painter.fillRect(event.rect(), palette["panel_blur" if blurred else "panel"])
            highlight = QLinearGradient(0, 0, 0, self.height())
            highlight.setColorAt(0, palette["grad_top"])
            highlight.setColorAt(1, palette["grad_bottom"])
            painter.fillRect(event.rect(), highlight)
            painter.fillRect(0, self.height() - 1, self.width(), 1, palette["border"])
        else:
            # A camada quase invisível mantém as áreas vazias clicáveis.
            painter.fillRect(event.rect(), QColor(0, 0, 0, 1))

    def contextMenuEvent(self, event):
        taskbar = explorer_taskbar()
        if not taskbar:
            event.ignore()
            return
        rect = wintypes.RECT()
        if not user32.GetWindowRect(int(self.winId()), ctypes.byref(rect)):
            event.ignore()
            return
        scale = self.devicePixelRatio()
        x = rect.left + round(event.pos().x() * scale)
        y = rect.top + round(event.pos().y() * scale)
        coords = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        if user32.PostMessageW(taskbar, 0x007B, taskbar, coords):  # WM_CONTEXTMENU
            self.context_owner = taskbar
            self.context_opened = False
            self.context_deadline = time.monotonic() + 1
            self.context_timer.start()
            event.accept()
        else:
            event.ignore()

    def dismiss_native_menu(self):
        owner_thread = user32.GetWindowThreadProcessId(self.context_owner, None)
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        menu = None
        visible = False
        while menu := user32.FindWindowExW(None, menu, "#32768", None):
            if (not user32.IsWindowVisible(menu)
                    or user32.GetWindowThreadProcessId(menu, None) != owner_thread):
                continue
            visible = True
            rect = wintypes.RECT()
            if (user32.GetWindowRect(menu, ctypes.byref(rect))
                    and rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom):
                self.context_opened = True
                return

        if not visible:
            if self.context_opened or time.monotonic() >= self.context_deadline:
                self.context_timer.stop()
            return

        self.context_opened = True
        if any(user32.GetAsyncKeyState(button) & 0x8000 for button in (1, 2, 4)):
            user32.PostMessageW(self.context_owner, 0x001F, 0, 0)  # WM_CANCELMODE
            self.context_timer.stop()

    def refresh_clock(self):
        if self.surface_blur is not None and self.surface_blur.isVisible():
            self.surface_blur.stay_below(int(self.winId()))
        now = QDateTime.currentDateTime()
        self.clock.setText(self.locale.toString(now, "ddd d 'de' MMM  ·  HH:mm"))

    def refresh_status(self):
        status = PowerStatus()
        has_level = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)) and status.percent <= 100
        self.battery.setVisible(bool(has_level))
        if has_level:
            charging = status.flags != 255 and bool(status.flags & 8)
            self.battery_symbol.percent = status.percent
            self.battery_symbol.charging = charging
            self.battery_symbol.update()
            self.battery_percent.setText(f"{status.percent}%")
            self.battery.setToolTip("Bateria carregando" if charging else "Nível da bateria")
        if self.wifi.state() == QProcess.NotRunning:
            self.wifi.start("netsh.exe", ["wlan", "show", "interfaces"])

    def refresh_network(self):
        output = bytes(self.wifi.readAllStandardOutput())
        signals = re.findall(rb"(?m)^\s*[^:\r\n]+:\s*(\d{1,3})%\s*$", output)
        levels = [int(signal) for signal in signals if int(signal) <= 100]
        if levels:
            level = max(levels)
            self.network_symbol.percent = level
            self.network_symbol.connected = True
            text, tooltip = "", f"Sinal Wi-Fi: {level}%"
        else:
            active = any(
                iface.flags() & QNetworkInterface.IsRunning and iface.addressEntries()
                and iface.type() in (QNetworkInterface.Ethernet, QNetworkInterface.Wifi)
                for iface in QNetworkInterface.allInterfaces()
            )
            self.network_symbol.percent = None
            self.network_symbol.connected = active
            text = "Rede" if active else "Offline"
            tooltip = "Estado da conexão de rede"
        self.network_symbol.update()
        self.network_text.setText(text)
        self.network_text.setVisible(bool(text))
        self.network.setToolTip(tooltip)
        self.network_symbol.setToolTip(f"{tooltip} · Ver conexões disponíveis")
        self.network.show()

    @staticmethod
    def find_tray_area():
        tray = explorer_taskbar()
        return user32.FindWindowExW(tray, None, "TrayNotifyWnd", None) if tray else 0

    @classmethod
    def find_tray_button(cls):
        area = cls.find_tray_area()
        return user32.GetDlgItem(area, 1502) if area else 0

    @classmethod
    def restore_interrupted_tray(cls):
        popup = user32.FindWindowW("NotifyIconOverflowWindow", None)
        alpha, flags = wintypes.BYTE(), wintypes.DWORD()
        if not (popup and user32.GetLayeredWindowAttributes(
                popup, None, ctypes.byref(alpha), ctypes.byref(flags)) and flags.value == 0x02
                and alpha.value in (1, 237)):
            return
        button = cls.find_tray_button()
        if button and user32.IsWindowVisible(popup):
            user32.PostMessageW(button, 0x00F5, 0, 0)
        style = user32.GetWindowLongPtrW(popup, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(popup, GWL_EXSTYLE, style & ~WS_EX_LAYERED)
        user32.SetWindowRgn(popup, None, False)

    def open_tray(self):
        if self.background_tray.isVisible():
            self.background_tray.close()
        else:
            self.volume.popup.close()
            self.background_tray.open(self.tray)

    def reset_tray_session(self):
        self.background_tray.close()


class WelcomeIllustration(QWidget):
    def __init__(self, group, light):
        super().__init__()
        self.group = group
        self.light = light
        self.page = 0
        self.setMinimumHeight(128)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        tile = QColor("#dce6f4" if self.light else "#2c3a51")
        chrome = QColor("#f8faff" if self.light else "#222a38")
        edge = QColor("#aabbd3" if self.light else "#526585")
        accent = QColor("#2861b3" if self.light else "#a0c8ff")
        painter.setPen(Qt.NoPen)
        painter.setBrush(tile)
        painter.drawRoundedRect(QRectF(self.rect()), 12, 12)
        center = self.width() / 2

        if self.page == 0:
            icons = [item.pixmap for item in self.group.items if not item.is_sep and not item.is_trash][:5]
            width = max(64, 20 + 47 * len(icons))
            strip = QRectF(center - width / 2, 43, width, 57)
            painter.setPen(QPen(edge, 1))
            painter.setBrush(chrome)
            painter.drawRoundedRect(strip, 14, 14)
            for index, icon in enumerate(icons):
                target = QRectF(strip.left() + 15 + 47 * index, 51, 37, 37)
                painter.drawPixmap(target, icon, QRectF(icon.rect()))
            painter.setPen(Qt.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(QPointF(center, 94), 2.3, 2.3)
        elif self.page == 1:
            for dx, selected in ((-104, False), (6, True)):
                box = QRectF(center + dx, 27, 98, 68)
                painter.setPen(QPen(accent if selected else edge, 1.5))
                painter.setBrush(chrome)
                painter.drawRoundedRect(box, 8, 8)
                painter.setPen(QPen(edge, 2, Qt.SolidLine, Qt.RoundCap))
                for row, length in ((0, 48), (1, 65), (2, 33)):
                    y = box.top() + 24 + 11 * row
                    painter.drawLine(QPointF(box.left() + 14, y), QPointF(box.left() + 14 + length, y))
        else:
            box = QRectF(center - 126, 25, 252, 76)
            painter.setPen(QPen(edge, 1))
            painter.setBrush(chrome)
            painter.drawRoundedRect(box, 10, 10)
            painter.setPen(QPen(edge, 1))
            painter.drawLine(QPointF(box.left() + 1, box.top() + 21),
                             QPointF(box.right() - 1, box.top() + 21))
            painter.setPen(Qt.NoPen)
            painter.setBrush(accent)
            for x in (center - 107, center - 94, center + 95, center + 108):
                painter.drawEllipse(QPointF(x, box.top() + 11), 3, 3)
            painter.drawRoundedRect(QRectF(center - 19, box.top() + 9, 38, 4), 2, 2)
            painter.setBrush(tile)
            painter.drawRoundedRect(QRectF(center - 41, box.bottom() - 15, 82, 8), 4, 4)


class WelcomeDialog(QDialog):
    def __init__(self, group):
        super().__init__(group.leader, Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setWindowTitle("Boas-vindas ao Pingo Dock")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_TranslucentBackground)
        dock = group.leader
        light = dock is not None and dock.theme_name == "light"
        self.pages = [
            ("Conheça o Pingo Dock",
             "Abra aplicativos pelo menu Iniciar ou pelos ícones do Dock. Para fixar um aplicativo, arraste o atalho dele até o Dock."
             if group.top_bar is not None else
             "Clique em um ícone para abrir ou voltar a um aplicativo. Arraste um atalho até o Dock para fixá-lo."),
            ("Encontre a janela certa",
             "Passe o mouse sobre um app aberto para ver suas janelas. Passe sobre uma miniatura para ampliá-la e clique para trazer a janela à frente."
             if group.top_bar is not None else
             "Clique com o botão direito sobre um app para ver suas janelas abertas. Selecione uma para trazê-la à frente."),
            ("Controles à mão",
             "Use Wi-Fi, volume e Bluetooth na barra superior. Clique com o botão direito no Dock para trocar a aparência da barra ou rever este guia."
             if group.top_bar is not None else
             "Clique com o botão direito no Dock para ajustar a aparência e rever este guia. Use o atalho "
             f"{group.cfg['hotkey']} para mostrar ou ocultar o Dock."),
        ]
        self.page = 0
        self.background = QColor("#f7f9fc" if light else "#20242d")
        self.border = QColor("#c6d0df" if light else "#525f74")
        ink = "#172234" if light else "#f4f6fb"
        quiet = "#47566d" if light else "#bbc6d9"
        button = "#e2e8f2" if light else "#343f51"
        hover = "#d2ddef" if light else "#43536c"
        self.setStyleSheet(f"""
            QLabel {{ color: {ink}; }}
            QLabel#guideBody, QLabel#guideStep {{ color: {quiet}; }}
            QPushButton {{ color: {ink}; background: {button}; border: 1px solid transparent;
                           border-radius: 7px; padding: 8px 13px; }}
            QPushButton:hover {{ background: {hover}; }}
            QPushButton:focus {{ border-color: #80aaff; }}
            QPushButton#guideNext {{ color: white; background: #315db0; }}
            QPushButton#guideNext:hover {{ background: #3e6dc5; }}
            QPushButton#guideSkip {{ background: transparent; color: {quiet}; }}
            QPushButton#guideSkip:hover {{ background: {hover}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 22, 25, 22)
        layout.setSpacing(12)
        self.illustration = WelcomeIllustration(group, light)
        layout.addWidget(self.illustration)
        self.title = QLabel()
        title_font = self.title.font()
        title_font.setPointSize(17)
        title_font.setWeight(QFont.DemiBold)
        self.title.setFont(title_font)
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.body = QLabel(wordWrap=True)
        self.body.setObjectName("guideBody")
        self.body.setMinimumHeight(67)
        self.body.setAlignment(Qt.AlignTop)
        layout.addWidget(self.body, 1)
        footer = QHBoxLayout()
        self.skip = QPushButton("Pular")
        self.skip.setObjectName("guideSkip")
        self.skip.clicked.connect(self.reject)
        footer.addWidget(self.skip)
        self.step = QLabel()
        self.step.setObjectName("guideStep")
        footer.addWidget(self.step)
        footer.addStretch()
        self.back = QPushButton("Voltar")
        self.back.clicked.connect(lambda: self.set_page(self.page - 1))
        footer.addWidget(self.back)
        self.next = QPushButton()
        self.next.setObjectName("guideNext")
        self.next.setDefault(True)
        self.next.clicked.connect(self.advance)
        footer.addWidget(self.next)
        layout.addLayout(footer)
        self.set_page(0)
        screen = dock.screen_ref if dock is not None else QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.resize(min(540, area.width()), 326)
        self.move(area.center() - self.rect().center())

    def set_page(self, page):
        self.page = page
        title, body = self.pages[page]
        self.title.setText(title)
        self.body.setText(body)
        self.step.setText(f"{page + 1} de {len(self.pages)}")
        self.back.setVisible(page > 0)
        self.next.setText("Começar" if page == len(self.pages) - 1 else "Próximo")
        self.illustration.page = page
        self.illustration.update()

    def advance(self):
        if self.page == len(self.pages) - 1:
            self.accept()
        else:
            self.set_page(self.page + 1)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(self.background)
        painter.setPen(QPen(self.border, 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 16, 16)


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if SHELL_PROTOTYPE and not SHELL_PREVIEW:
        _proto(user32.GetShellWindow, HWND)
        if user32.GetShellWindow() or explorer_taskbar():
            sys.exit("O protótipo precisa de uma sessão de teste sem o shell do Explorer. "
                     "Use --preview para validar somente a interface.")
    if already_running():
        return
    if not (DEV_MODE or TRAY_PREVIEW or SHELL_PROTOTYPE):
        migrate_from_macdock()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    configure_ui_font(app)
    app.setQuitOnLastWindowClosed(False)
    group = DockGroup()
    group.build()          # um dock por tela (ou só na principal)
    group.watch_screens()  # monitor ligado/desligado → reorganiza
    if DESKTOP_MODE:
        if not SHELL_PROTOTYPE:
            TopBar.restore_interrupted_tray()
        group.top_bar = TopBar(group)
        group.top_bar.show()
        if not SHELL_PREVIEW:
            host = TrayHost()
            group.top_bar.background_tray.host = host
            app.aboutToQuit.connect(host.close)
        if not SHELL_PROTOTYPE:
            group.top_bar.enable_work_area()
        if not SHELL_PREVIEW:
            start_key = StartKeyHook(group)
            app.aboutToQuit.connect(start_key.close)
    if FROZEN and not SHELL_PROTOTYPE:
        guard = Path(sys.executable).with_name("PingoDockTaskbar.exe")
        try:
            subprocess.Popen([str(guard), str(os.getpid()), str(int(group.top_bar.winId()))],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as error:
            log(f"não foi possível iniciar o controle da barra do Windows: {error}")
    if not group.cfg["welcome_seen"]:
        QTimer.singleShot(250, group.show_welcome)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
