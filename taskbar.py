"""Mantém a barra nativa oculta enquanto a interface do Pingo Dock está ativa."""

import ctypes
from ctypes import wintypes


class WindowsTaskbar:
    def __init__(self):
        self.user32 = ctypes.WinDLL("user32")
        self.enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = (self.enum_callback, wintypes.LPARAM)
        self.user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.ShowWindowAsync.argtypes = (wintypes.HWND, ctypes.c_int)
        self.hidden = set()

    def hide(self):
        name = ctypes.create_unicode_buffer(64)

        @self.enum_callback
        def visit(hwnd, _):
            self.user32.GetClassNameW(hwnd, name, len(name))
            if name.value in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                if self.user32.IsWindowVisible(hwnd) and self.user32.ShowWindowAsync(hwnd, 0):
                    self.hidden.add(hwnd)
            return True

        self.user32.EnumWindows(visit, 0)

    def restore(self):
        for hwnd in self.hidden:
            self.user32.ShowWindowAsync(hwnd, 4)  # SW_SHOWNOACTIVATE
        self.hidden.clear()
