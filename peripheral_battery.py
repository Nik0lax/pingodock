import ctypes as C
import re
import uuid
from ctypes import wintypes as W


class DeviceInfo(C.Structure):
    _fields_ = [("size", W.DWORD), ("class_id", C.c_byte * 16),
                ("instance", W.DWORD), ("reserved", C.c_size_t)]


class PropertyKey(C.Structure):
    _fields_ = [("guid", C.c_ubyte * 16), ("pid", W.DWORD)]


def key(guid, pid):
    return PropertyKey((C.c_ubyte * 16).from_buffer_copy(uuid.UUID(guid).bytes_le), pid)


BATTERY = key("104EA319-6EE2-4701-BD47-8DDBF425BBE5", 2)
CONNECTED = key("83DA6326-97A6-4088-9453-A1923F573B29", 15)
DEVICE_CLASS = key("2BD67D8B-8BEB-48D5-87E0-6CDA3428040A", 4)
NAME = key("A45C254E-DF1C-4EFD-8020-67D146A850E0", 14)
INSTANCE_ID = key("78C34FC8-104A-4ACA-9EA4-524D52996E57", 256)

setup = C.WinDLL("setupapi")
setup.SetupDiGetClassDevsW.argtypes = C.c_void_p, W.LPCWSTR, W.HWND, W.DWORD
setup.SetupDiGetClassDevsW.restype = W.HANDLE
setup.SetupDiEnumDeviceInfo.argtypes = W.HANDLE, W.DWORD, C.POINTER(DeviceInfo)
setup.SetupDiEnumDeviceInfo.restype = W.BOOL
setup.SetupDiGetDevicePropertyW.argtypes = (W.HANDLE, C.POINTER(DeviceInfo), C.POINTER(PropertyKey),
                                          C.POINTER(W.DWORD), C.c_void_p, W.DWORD,
                                          C.POINTER(W.DWORD), W.DWORD)
setup.SetupDiGetDevicePropertyW.restype = W.BOOL
setup.SetupDiDestroyDeviceInfoList.argtypes = (W.HANDLE,)
setup.SetupDiDestroyDeviceInfoList.restype = W.BOOL


def property_data(devices, info, prop):
    buffer = C.create_string_buffer(1024)
    kind, size = W.DWORD(), W.DWORD()
    if setup.SetupDiGetDevicePropertyW(devices, C.byref(info), C.byref(prop), C.byref(kind),
                                      buffer, len(buffer), C.byref(size), 0):
        return buffer.raw[:size.value]
    return b""


def string_value(value):
    return value.decode("utf-16-le", errors="ignore").rstrip("\0")


def peripheral_kind(name, device_class):
    major = (device_class >> 8) & 31
    if major in (1, 2):  # computador / telefone
        return None
    if major == 4 or re.search(r"headset|headphone|earbuds?|fone", name, re.I):
        return "headphones"
    if (major == 5 and device_class & 0x80) or re.search(r"mouse|trackball", name, re.I):
        return "mouse"
    if (major == 5 and device_class & 0x40) or re.search(r"keyboard|teclado", name, re.I):
        return "keyboard"
    if (major == 5 and (device_class >> 2) & 15 in (1, 2)
            or re.search(r"gamepad|controller|dualshock|dualsense|xbox|controle", name, re.I)):
        return "gamepad"
    return "device"


def connected_batteries():
    connected, levels = {}, {}
    for enumerator in ("BTHENUM", "BTHLE"):
        devices = setup.SetupDiGetClassDevsW(None, enumerator, None, 0x06)  # PRESENT | ALLCLASSES
        if not devices or devices == C.c_void_p(-1).value:
            continue
        try:
            info = DeviceInfo(size=C.sizeof(DeviceInfo))
            index = 0
            while setup.SetupDiEnumDeviceInfo(devices, index, C.byref(info)):
                index += 1
                instance = string_value(property_data(devices, info, INSTANCE_ID))
                root = re.match(r"[^\\]+\\DEV_([0-9A-F]{12})(?:\\|$)", instance, re.I)
                profile = re.search(r"&([0-9A-F]{12})(?:_[^\\]+)?$", instance, re.I)
                address = (root or profile)
                if not address:
                    continue
                identity = address[1].casefold()
                if battery := property_data(devices, info, BATTERY):
                    if battery[0] <= 100:
                        levels.setdefault(identity, battery[0])
                if root and property_data(devices, info, CONNECTED).startswith(b"\xff"):
                    name = string_value(property_data(devices, info, NAME))
                    device_class = int.from_bytes(property_data(devices, info, DEVICE_CLASS), "little")
                    category = peripheral_kind(name, device_class)
                    if name and category:
                        connected.setdefault(identity, (name, category))
        finally:
            setup.SetupDiDestroyDeviceInfoList(devices)
    priority = {"headphones": 0, "mouse": 1, "keyboard": 2, "gamepad": 3, "device": 4}
    found = [(name, category, levels[address]) for address, (name, category) in connected.items()
             if address in levels]
    return sorted(found, key=lambda item: (priority[item[1]], item[0].casefold()))
