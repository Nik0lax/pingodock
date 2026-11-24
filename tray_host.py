"""Experimental receiver for live Windows notification icon registrations."""
import ctypes as C
import os
from ctypes import wintypes as W

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage

user = C.WinDLL("user32", use_last_error=True)
kernel = C.WinDLL("kernel32")
WNDPROC = C.WINFUNCTYPE(W.LPARAM, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
ENUMPROC = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)


class WindowClass(C.Structure):
    _fields_ = [
        ("style", W.UINT), ("procedure", WNDPROC), ("class_extra", C.c_int),
        ("window_extra", C.c_int), ("instance", W.HINSTANCE), ("icon", W.HICON),
        ("cursor", W.HANDLE), ("background", W.HBRUSH), ("menu", W.LPCWSTR), ("name", W.LPCWSTR),
    ]


class CopyData(C.Structure):
    _fields_ = [("kind", W.WPARAM), ("size", W.DWORD), ("data", C.c_void_p)]


# The shell's wire format keeps 32-bit USER handles even in 64-bit processes.
class IconPacket(C.Structure):
    _fields_ = [
        ("signature", W.DWORD), ("operation", W.DWORD), ("size", W.DWORD),
        ("window", W.DWORD), ("identifier", W.DWORD), ("flags", W.DWORD),
        ("callback", W.DWORD), ("icon", W.DWORD), ("tip", W.WCHAR * 128),
        ("state", W.DWORD), ("state_mask", W.DWORD), ("info", W.WCHAR * 256),
        ("version", W.DWORD), ("info_title", W.WCHAR * 64), ("info_flags", W.DWORD),
        ("guid", C.c_ubyte * 16), ("balloon_icon", W.DWORD),
    ]


class IconLocationRequest(C.Structure):
    _fields_ = [
        ("signature", W.DWORD), ("operation", W.DWORD), ("size", W.DWORD),
        ("padding", W.DWORD), ("window", W.DWORD), ("identifier", W.DWORD),
        ("guid", C.c_ubyte * 16),
    ]


def pack_point(x, y):
    return (x & 0xffff) | ((y & 0xffff) << 16)


def bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = arguments


bind(kernel, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
bind(user, "RegisterClassW", W.ATOM, C.POINTER(WindowClass))
bind(user, "UnregisterClassW", W.BOOL, W.LPCWSTR, W.HINSTANCE)
bind(user, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
     C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p)
bind(user, "DestroyWindow", W.BOOL, W.HWND)
bind(user, "DefWindowProcW", W.LPARAM, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
bind(user, "EnumWindows", W.BOOL, ENUMPROC, W.LPARAM)
bind(user, "GetClassNameW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
bind(user, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
bind(user, "IsWindow", W.BOOL, W.HWND)
bind(user, "SetWindowPos", W.BOOL, W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
bind(user, "FindWindowW", W.HWND, W.LPCWSTR, W.LPCWSTR)
bind(user, "RegisterWindowMessageW", W.UINT, W.LPCWSTR)
bind(user, "SendNotifyMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
bind(user, "SendMessageTimeoutW", W.LPARAM, W.HWND, W.UINT, W.WPARAM, W.LPARAM,
     W.UINT, W.UINT, C.POINTER(W.WPARAM))
bind(user, "PostMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
bind(user, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
bind(user, "AllowSetForegroundWindow", W.BOOL, W.DWORD)


def owner(hwnd):
    pid = W.DWORD()
    user.GetWindowThreadProcessId(hwnd, C.byref(pid))
    return pid.value


def explorer_taskbar():
    native = None

    @ENUMPROC
    def collect(hwnd, _):
        nonlocal native
        if owner(hwnd) != os.getpid():
            name = C.create_unicode_buffer(32)
            user.GetClassNameW(hwnd, name, len(name))
            if name.value == "Shell_TrayWnd":
                native = hwnd
                return False
        return True

    user.EnumWindows(collect, 0)
    return native


class TrayIcon:
    def __init__(self, hwnd, identifier):
        self.hwnd, self.identifier = hwnd, identifier
        self.pid = owner(hwnd)
        self.callback = 0
        self.version = 0
        self.hidden = False
        self.label = ""
        self.image = QImage()
        self.bounds = None

    def click(self, right):
        if not self.callback or owner(self.hwnd) != self.pid:
            return False
        user.AllowSetForegroundWindow(self.pid)
        events = [0x0204, 0x0205] if right else [0x0201, 0x0202]
        if self.version >= 3:
            events.append(0x007B if right else 0x0400)  # WM_CONTEXTMENU / NIN_SELECT
        point = W.POINT()
        user.GetCursorPos(C.byref(point))
        if self.bounds:
            left, top, bottom_right_x, bottom_right_y = self.bounds
            if not (left <= point.x < bottom_right_x and top <= point.y < bottom_right_y):
                point.x, point.y = (left + bottom_right_x) // 2, (top + bottom_right_y) // 2
        wparam = pack_point(point.x, point.y) if self.version >= 4 else self.identifier
        icon_word = (self.identifier & 0xffff) << 16 if self.version >= 4 else 0
        return all([user.PostMessageW(self.hwnd, self.callback, wparam, event | icon_word)
                    for event in events])


class TrayHost:
    def __init__(self):
        self.icons = {}
        self.revision = 0
        self.hwnd = None
        self.native = explorer_taskbar()
        self.procedure = WNDPROC(self.receive)
        self.instance = kernel.GetModuleHandleW(None)
        self.message = user.RegisterWindowMessageW("TaskbarCreated")
        wc = WindowClass(procedure=self.procedure, instance=self.instance, name="Shell_TrayWnd")
        self.registered = bool(user.RegisterClassW(C.byref(wc)))
        self.check = QTimer(interval=1000)
        self.check.timeout.connect(self.ensure_receiver)
        self.recovery = QTimer(singleShot=True, interval=400)
        self.recovery.timeout.connect(self.recover)
        if not self.registered:
            return
        self.hwnd = user.CreateWindowExW(0x88, wc.name, "Pingo Dock notification receiver",
                                         0x80000000, 0, 0, 0, 0, None, None, self.instance, None)
        if self.hwnd:
            self.raise_receiver()
            self.announce()
            self.check.start()

    def announce(self):
        if self.message:
            user.SendNotifyMessageW(0xffff, self.message, 0, 0)

    def raise_receiver(self):
        user.SetWindowPos(self.hwnd, C.c_void_p(-1), 0, 0, 0, 0, 0x13)

    def ensure_receiver(self):
        if not self.hwnd:
            return
        native = self.native
        if not native or not user.IsWindow(native):
            self.native = explorer_taskbar()
        first = user.FindWindowW("Shell_TrayWnd", None)
        if first != self.hwnd:
            self.native = explorer_taskbar()
            self.raise_receiver()
        if (self.native != native or first != self.hwnd) and not self.recovery.isActive():
            self.recovery.start()
        self.prune()

    def recover(self):
        if not self.hwnd:
            return
        self.native = explorer_taskbar()
        self.raise_receiver()
        self.prune()
        if user.FindWindowW("Shell_TrayWnd", None) == self.hwnd:
            self.announce()

    def close(self):
        self.check.stop()
        self.recovery.stop()
        if self.hwnd:
            user.DestroyWindow(self.hwnd)
            self.hwnd = None
            self.announce()
        if self.registered:
            user.UnregisterClassW("Shell_TrayWnd", self.instance)
            self.registered = False
        self.icons.clear()

    def prune(self):
        for key, icon in tuple(self.icons.items()):
            if owner(icon.hwnd) != icon.pid:
                self.icons.pop(key, None)
                self.revision += 1

    def items(self):
        self.prune()
        return [icon for icon in self.icons.values() if icon.callback and not icon.hidden]

    def receive(self, hwnd, msg, wparam, lparam):
        if msg == 0x004A and lparam:  # WM_COPYDATA
            packet = CopyData.from_address(lparam)
            accepted = False
            minimum = IconPacket.tip.offset
            if packet.kind == 1 and packet.data and minimum <= packet.size <= 2048:
                data = C.string_at(packet.data, min(packet.size, C.sizeof(IconPacket)))
                icon = IconPacket.from_buffer_copy(data.ljust(C.sizeof(IconPacket), b"\0"))
                accepted = self.update(icon)
            elif packet.kind == 3 and packet.data and packet.size >= C.sizeof(IconLocationRequest):
                request = IconLocationRequest.from_address(packet.data)
                guid = bytes(request.guid)
                icon = self.icons.get(guid) if any(guid) else self.icons.get((request.window, request.identifier))
                if icon and icon.bounds and request.operation in (1, 2):
                    offset = 0 if request.operation == 1 else 2
                    return pack_point(*icon.bounds[offset:offset + 2])
            # Keep the Explorer's registrations current for recovery and let
            # Windows continue handling notifications the Pingo does not show.
            if self.native and user.IsWindow(self.native):
                result = W.WPARAM()
                if user.SendMessageTimeoutW(self.native, msg, wparam, lparam, 0x02, 100, C.byref(result)):
                    return 1 if accepted else C.c_ssize_t(result.value).value
            return int(accepted)
        return user.DefWindowProcW(hwnd, msg, wparam, lparam)

    def update(self, packet):
        pid = owner(packet.window)
        if packet.operation not in (0, 1, 2, 4) or not pid:
            return False
        guid = bytes(packet.guid)
        pair = (packet.window, packet.identifier)
        key = guid if packet.flags & 0x20 and any(guid) else pair
        if key not in self.icons and key == pair:
            key = next((entry for entry, icon in self.icons.items()
                        if (icon.hwnd, icon.identifier) == pair and icon.pid == pid), key)
        if packet.operation == 2:
            if self.icons.pop(key, None) is not None:
                self.revision += 1
            return True
        icon = self.icons.get(key)
        if icon is None or (icon.hwnd, icon.pid) != (packet.window, pid):
            icon = self.icons[key] = TrayIcon(packet.window, packet.identifier)
            self.revision += 1
        if packet.operation == 4:
            icon.version = packet.version
            return True

        previous = icon.callback, icon.label, icon.hidden, icon.image
        if packet.flags & 1 and packet.callback >= 0x0400:
            icon.callback = packet.callback
        if packet.flags & 2 and packet.icon:
            image = QImage.fromHICON(packet.icon)
            if not image.isNull():
                icon.image = image
        if packet.flags & 4:
            icon.label = packet.tip.strip()
        if packet.flags & 8 and packet.state_mask & 1:
            icon.hidden = bool(packet.state & 1)
        if previous != (icon.callback, icon.label, icon.hidden, icon.image):
            self.revision += 1
        return True
