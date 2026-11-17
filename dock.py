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

import ctypes
import json
import math
import os
import sys
import threading
import time
import winreg
from ctypes import wintypes
from pathlib import Path

# Rodando pelo Python "puro" (sem o lançador do .venv, que fica como um processo
# extra na memória): usa as bibliotecas instaladas no .venv do projeto.
_VENV_SITE = Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages"
if _VENV_SITE.is_dir() and str(_VENV_SITE) not in sys.path:
    import site

    site.addsitedir(str(_VENV_SITE))

from PySide6.QtCore import QFileInfo, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFileIconProvider,
    QInputDialog,
    QMenu,
    QWidget,
)

APP_NAME = "PingoDock"     # nome técnico: executável, pastas, registro
APP_TITLE = "Pingo Dock"   # nome que aparece para o usuário
APP_VERSION = "1.3.0"  # o build.py lê daqui a versão do instalador
FROZEN = getattr(sys, "frozen", False)  # rodando como PingoDock.exe (instalado)
APP_DIR = Path(__file__).resolve().parent
# Instalado, a pasta do programa pode não ser gravável: configuração e log vão
# para %APPDATA%\PingoDock. Em desenvolvimento, ficam na pasta do projeto.
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME if FROZEN else APP_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"

PAD_X = 10          # espaço interno lateral do painel
PAD_TOP = 9         # espaço interno acima dos ícones
PAD_BOTTOM = 12     # espaço interno abaixo dos ícones (onde fica a bolinha)
MARGIN_BOTTOM = 6   # distância do painel até a borda da tela
RADIUS = 2.6        # alcance da ampliação, medido em "ícones"
ICON_RES = 256      # resolução em que os ícones são extraídos (depois reduzidos)
BOUNCE_TIME = 1.0   # duração do pulo ao abrir um app (s)
SEP_SLOT = 6        # largura do "espaço" de um separador (além do espaçamento normal)
LEARN_TIME = 15.0   # por quanto tempo, após abrir um app, tentamos descobrir seu processo real
NET_RETRY_SECS = 10.0  # intervalo entre tentativas de carregar ícones da rede
UPDATE_EVERY_SECS = 6 * 3600  # de quanto em quanto tempo procurar versão nova
HOTKEY_ID = 1

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


def _proto(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = list(argtypes)


_proto(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_proto(user32.EnumChildWindows, wintypes.BOOL, HWND, WNDENUMPROC, wintypes.LPARAM)
_proto(user32.GetWindowTextW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_proto(user32.GetWindowThreadProcessId, wintypes.DWORD, HWND, ctypes.POINTER(wintypes.DWORD))
_proto(user32.IsWindowVisible, wintypes.BOOL, HWND)
_proto(user32.IsIconic, wintypes.BOOL, HWND)
_proto(user32.IsZoomed, wintypes.BOOL, HWND)
_proto(user32.GetWindow, HWND, HWND, wintypes.UINT)
_proto(user32.GetWindowLongPtrW, ctypes.c_ssize_t, HWND, ctypes.c_int)
_proto(user32.GetWindowTextLengthW, ctypes.c_int, HWND)
_proto(user32.GetClassNameW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
_proto(user32.ShowWindow, wintypes.BOOL, HWND, ctypes.c_int)
_proto(user32.SetForegroundWindow, wintypes.BOOL, HWND)
_proto(user32.GetForegroundWindow, HWND)
_proto(user32.GetWindowRect, wintypes.BOOL, HWND, ctypes.POINTER(wintypes.RECT))
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
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x2
MONITOR_DEFAULTTONEAREST = 2

SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


shell32 = ctypes.WinDLL("shell32")
gdi32 = ctypes.WinDLL("gdi32")

WM_CLOSE = 0x0010
WM_HOTKEY = 0x0312
_proto(user32.PostMessageW, wintypes.BOOL, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_proto(user32.RegisterHotKey, wintypes.BOOL, HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_proto(user32.UnregisterHotKey, wintypes.BOOL, HWND, ctypes.c_int)
_proto(user32.SetWindowRgn, ctypes.c_int, HWND, wintypes.HRGN, wintypes.BOOL)
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
_proto(kernel32.OpenProcess, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_proto(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
_proto(kernel32.QueryFullProcessImageNameW, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
       wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
_proto(kernel32.GetCurrentProcess, wintypes.HANDLE)
_proto(kernel32.SetProcessWorkingSetSize, wintypes.BOOL, wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t)


def process_name(pid: int) -> str:
    """Nome do executável do processo (ex.: "chrome.exe"), em minúsculas.
    O acesso 'limitado' funciona até para processos de administrador."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)


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
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\PingoDock_SingleInstance")
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
    "icon_size": 56,
    "magnification": 1.85,
    "magnify": True,
    "spacing": 8,
    "autohide": False,
    "all_screens": False,   # um dock em cada monitor
    "theme": "auto",        # "auto" (segue o Windows), "dark" ou "light"
    "blur": True,           # fundo desfocado atrás do painel
    "hotkey": "Ctrl+Alt+D", # mostrar/esconder o dock
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
            return cfg
        except (OSError, ValueError):
            pass
    cfg["apps"] = default_apps()
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


def extract_icon(file: str, index: int = 0, size: int = ICON_RES) -> QPixmap:
    """Ícone em alta resolução direto do .exe/.dll/.ico (sem a seta de atalho)."""
    hicon = wintypes.HICON()
    icon_id = wintypes.UINT()
    count = user32.PrivateExtractIconsW(file, index, size, size, ctypes.byref(hicon),
                                        ctypes.byref(icon_id), 1, 0)
    if count in (0, 0xFFFFFFFF) or not hicon:
        return QPixmap()
    try:
        return QPixmap.fromImage(QImage.fromHICON(hicon.value))
    finally:
        user32.DestroyIcon(hicon)


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


def load_icon(path: str, target: str, provider: QFileIconProvider, px: int,
              is_folder: bool = False) -> tuple[QPixmap, list[str]]:
    """Ícone com no máximo `px` pixels de lado: extrai em alta resolução e reduz
    com suavização para o tamanho máximo em que ele aparece na tela (economiza
    memória sem perder nitidez).
    Devolve também os caminhos de rede que estavam inacessíveis (ex.: o dock
    abriu no logon antes da rede ficar pronta) — o dock tenta de novo depois."""
    missing: list[str] = []
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
    if target.lower().endswith(ICON_FILE_EXTS):
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

    def __init__(self, name: str, path: str, provider: QFileIconProvider, icon_px: int,
                 match: list[str] | None = None) -> None:
        self.name = name
        self.path = path
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
        self.pixmap, self.icon_missing = load_icon(path, target, provider, icon_px, self.is_folder)
        self.running = False
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
                                                   self.is_folder)

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
        return data


class Separator:
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


def make_item(data: dict, provider: QFileIconProvider, icon_px: int):
    if data.get("type") == "separator":
        return Separator()
    if data.get("type") == "trash":
        return TrashItem(icon_px)
    return DockItem(data["name"], data["path"], provider, icon_px, data.get("match"))


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

    def __init__(self) -> None:
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
        self.region_key = None

    def place(self, rect: QRect, dock_hwnd: int) -> None:
        if rect.width() < 8 or rect.height() < 8:
            if self.isVisible():
                self.hide()
            return
        if rect != self.geometry():
            self.setGeometry(rect)
        newly_shown = not self.isVisible()
        if newly_shown:
            self.show()
        hwnd = int(self.winId())
        if not self.blur_on:
            self.blur_on = enable_blur(hwnd)
        dpr = self.devicePixelRatioF()
        key = (rect.width(), rect.height(), dpr)
        if key != self.region_key:
            self.region_key = key
            w, h = round(rect.width() * dpr), round(rect.height() * dpr)
            d = round(2 * PANEL_RADIUS * dpr)
            user32.SetWindowRgn(hwnd, gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d), True)
        if newly_shown:
            self.stay_below(dock_hwnd)

    def stay_below(self, dock_hwnd: int) -> None:
        if self.isVisible():
            user32.SetWindowPos(int(self.winId()), dock_hwnd, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


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
        self._rebuild_timer: QTimer | None = None

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
        self.cfg["apps"] = [it.to_json() for it in self.items]
        save_config(self.cfg)

    # ---- um dock por tela ------------------------------------------------
    def target_screens(self) -> list:
        primary = QGuiApplication.primaryScreen()
        if not self.cfg.get("all_screens"):
            return [primary]
        return [primary] + [s for s in QGuiApplication.screens() if s is not primary]

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
        self.setScreen(self.screen_ref)
        self.pressed_pixmap: QPixmap | None = None  # versão escurecida, só durante o clique

        self.theme_name = self.resolve_theme()
        self.blur: BlurBehind | None = BlurBehind() if self.cfg["blur"] else None
        self.forced: str | None = None    # atalho de teclado: "show", "hide" ou None
        self.hotkey_ok: bool | None = None
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

        self.label_font = QFont("Segoe UI", 9)
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

        self.screen_ref.availableGeometryChanged.connect(self.reposition)
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
        for timer in (self.anim_timer, self.slow_timer, self.cover_timer):
            timer.stop()
        if self.hotkey_ok:
            user32.UnregisterHotKey(int(self.winId()), HOTKEY_ID)
            self.hotkey_ok = None
        if self.blur is not None:
            self.blur.close()
            self.blur.deleteLater()
            self.blur = None
        try:
            self.screen_ref.availableGeometryChanged.disconnect(self.reposition)
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
        visible = panel.intersected(QRectF(self.rect())).adjusted(1, 1, -1, -1)
        if not self.isVisible() or self.reveal < 0.02 or visible.height() < 8:
            if self.blur.isVisible():
                self.blur.hide()
            return
        top_left = self.mapToGlobal(QPoint(round(visible.x()), round(visible.y())))
        self.blur.place(QRect(top_left, QSize(round(visible.width()), round(visible.height()))),
                        int(self.winId()))

    # ---- atalho de teclado (mostrar/esconder) ---------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.hotkey_ok is None and self.is_leader:  # o atalho é global: um registro só
            self.register_hotkey()

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
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.toggle_visibility()
                return True, 0
        return False, 0

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
        return float(self.cfg["icon_size"])

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
        avail = self.screen_ref.availableGeometry()
        w = min(w, avail.width())
        self.setGeometry(avail.x() + (avail.width() - w) // 2, avail.bottom() - h + 1, w, h)
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
        if self.forced == "hide" and not self.menu_open:
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

    @staticmethod
    def item_windows(item: DockItem, windows: list[tuple[int, int, str]]) -> list[int]:
        return [hwnd for hwnd, _pid, name in windows if name in item.match_names]

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
        changed = False
        for it in self.items:
            if it.is_sep:
                continue
            # "Aberto" = tem pelo menos uma janela (como a barra de tarefas do Windows).
            # Assim o Explorador (que sempre roda por trás) e apps que ficam em
            # segundo plano, como o Edge, só ganham bolinha com janela aberta.
            running = bool(self.item_windows(it, windows))
            if running != it.running:
                it.running = running
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
        hide_for_fullscreen = self.cover == "fullscreen" and self.forced != "show"
        if hide_for_fullscreen and self.isVisible():
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
            pressed = i == self.press_index and not self.dragging
            pm = self.pressed_pixmap if pressed and self.pressed_pixmap else item.pixmap
            p.drawPixmap(r, pm, QRectF(pm.rect()))
            if item.is_folder:
                self.draw_folder_name(p, r, clean_name(item.name))
            if item.running:
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
        elif self.hover > 0.6 and not self.menu_open:
            idx = self.hit(QPointF(self.mapFromGlobal(QCursor.pos())))
            if idx is not None and not self.items[idx].is_sep:
                r = rects[idx]
                self.draw_label(p, clean_name(self.items[idx].name), r.center().x(), r.top() - 8)

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
        font = QFont("Segoe UI")
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
        if item.is_trash:
            open_recycle_bin()
            return
        windows = self.item_windows(item, self.named_windows())
        if windows:
            visible = [w for w in windows if not user32.IsIconic(w)]
            bring_to_front((visible or windows)[0])
        else:
            self.launch(item)

    def launch(self, item: DockItem) -> None:
        if not item.running and not item.is_web and not item.is_folder:
            item.learn_t0 = time.monotonic()
            item.learn_before = {h for h, _ in app_windows()}
        try:
            if item.path.lower().endswith(".exe"):
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

    def add_paths(self, paths: list[str], index: int | None = None) -> None:
        index = len(self.items) if index is None else index
        for path in paths:
            path = os.path.normpath(path)
            name = Path(path).stem if Path(path).suffix else Path(path).name
            self.items.insert(index, DockItem(name, path, self.provider, self.icon_px))
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
        self.items.insert(index, Separator())
        self.save()
        self.reposition()

    def remove_item(self, item) -> None:
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
        idx = self.hit(QPointF(e.pos()))
        style = self.pal["menu"]
        menu = QMenu(self)
        menu.setStyleSheet(style)

        if idx is not None and self.items[idx].is_sep:
            sep = self.items[idx]
            menu.addAction("Remover separador", lambda: self.remove_item(sep))
            menu.addSeparator()
        elif idx is not None and self.items[idx].is_trash:
            trash = self.items[idx]
            menu.addAction("Abrir", open_recycle_bin)
            empty = menu.addAction("Esvaziar Lixeira", empty_recycle_bin)
            empty.setEnabled(trash.full)
            menu.addAction("Remover do Dock", lambda: self.remove_item(trash))
            menu.addSeparator()
        elif idx is not None:
            item = self.items[idx]
            title = menu.addAction(item.name)
            title.setEnabled(False)
            menu.addSeparator()
            # Janelas abertas do app: clicar traz aquela janela para frente.
            windows = self.item_windows(item, self.named_windows())
            for hwnd in windows[:15]:
                text = window_title(hwnd) or clean_name(item.name)
                text = text if len(text) <= 60 else text[:57] + "…"
                if user32.IsIconic(hwnd):
                    text += "   (minimizada)"
                menu.addAction(text.replace("&", "&&"), lambda h=hwnd: bring_to_front(h))
            if windows:
                menu.addSeparator()
            menu.addAction("Nova janela" if item.running else "Abrir", lambda: self.launch(item))
            menu.addAction("Mostrar no Explorador",
                           lambda: os.startfile("explorer.exe", arguments=f'/select,"{item.path}"'))
            menu.addAction("Renomear…", lambda: self.rename_item(item))
            menu.addAction("Remover do Dock", lambda: self.remove_item(item))
            if windows:
                menu.addSeparator()
                label = "Fechar" if len(windows) == 1 else f"Fechar {len(windows)} janelas"
                menu.addAction(label, lambda ws=list(windows): [close_window(h) for h in ws])
            menu.addSeparator()

        menu.addAction("Adicionar aplicativo…", self.pick_apps)
        # Clicou num ícone: separador entra logo depois dele; senão, onde clicou.
        sep_index = idx + 1 if idx is not None else self.insert_index(e.pos().x())
        menu.addAction("Adicionar separador", lambda: self.add_separator(sep_index))
        if not any(it.is_trash for it in self.items):
            menu.addAction("Adicionar Lixeira", self.add_trash)

        size_menu = menu.addMenu("Tamanho dos ícones")
        size_menu.setStyleSheet(style)
        group = QActionGroup(size_menu)
        for label, size in ICON_SIZES.items():
            act = QAction(label, size_menu, checkable=True, checked=self.cfg["icon_size"] == size)
            act.triggered.connect(lambda _=False, s=size: self.set_option("icon_size", s))
            group.addAction(act)
            size_menu.addAction(act)

        magnify = menu.addAction("Ampliação")
        magnify.setCheckable(True)
        magnify.setChecked(self.cfg["magnify"])
        magnify.toggled.connect(lambda on: self.set_option("magnify", on))

        autohide = menu.addAction("Ocultar automaticamente")
        autohide.setCheckable(True)
        autohide.setChecked(self.cfg["autohide"])
        autohide.toggled.connect(lambda on: self.set_option("autohide", on))

        look_menu = menu.addMenu("Aparência")
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

        screens = menu.addAction("Mostrar em todas as telas")
        screens.setCheckable(True)
        screens.setChecked(bool(self.cfg.get("all_screens")))
        screens.setEnabled(len(QGuiApplication.screens()) > 1 or bool(self.cfg.get("all_screens")))
        screens.toggled.connect(lambda on: self.set_option("all_screens", on))

        startup = menu.addAction("Iniciar com o Windows")
        startup.setCheckable(True)
        startup.setChecked(startup_enabled())
        startup.toggled.connect(set_startup)

        menu.addSeparator()
        lead = self.group.leader or self  # atalho e atualizações ficam com o dock principal
        hide_text = "Ocultar o Dock"
        if lead.hotkey_ok:
            hide_text += f"\t{self.cfg['hotkey']}"
        menu.addAction(hide_text, lambda: self.group.set_forced("hide"))
        if lead.update_info:
            menu.addAction(f"Atualizar para a versão {lead.update_info['version']}…", lead.start_update)
        elif update_source(self.cfg):
            menu.addAction("Procurar atualizações", lambda: lead.check_updates(manual=True))
        about = menu.addAction(f"{APP_TITLE} {APP_VERSION}")
        about.setEnabled(False)
        menu.addAction("Sair do Dock", QApplication.quit)

        self.menu_open = True
        menu.exec(e.globalPos())
        self.menu_open = False

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


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if already_running():
        return
    migrate_from_macdock()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    group = DockGroup()
    group.build()          # um dock por tela (ou só na principal)
    group.watch_screens()  # monitor ligado/desligado → reorganiza
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
