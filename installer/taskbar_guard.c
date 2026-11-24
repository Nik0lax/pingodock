#include <windows.h>
#include <stdlib.h>
#include <wchar.h>
#include <wctype.h>

typedef struct HiddenBar {
    HWND window;
    DWORD owner;
    struct HiddenBar *next;
} HiddenBar;

static BOOL is_taskbar(HWND window) {
    wchar_t name[64];
    return GetClassNameW(window, name, ARRAYSIZE(name)) &&
        (wcscmp(name, L"Shell_TrayWnd") == 0 ||
         wcscmp(name, L"Shell_SecondaryTrayWnd") == 0);
}

static BOOL CALLBACK hide_taskbar(HWND window, LPARAM data) {
    HiddenBar **hidden = (HiddenBar **)data;
    DWORD owner = 0;
    if (!IsWindowVisible(window) || !is_taskbar(window) ||
        !GetWindowThreadProcessId(window, &owner))
        return TRUE;

    for (HiddenBar *bar = *hidden; bar; bar = bar->next) {
        if (bar->window == window && bar->owner == owner) {
            ShowWindowAsync(window, SW_HIDE);
            return TRUE;
        }
    }
    HiddenBar *bar = HeapAlloc(GetProcessHeap(), 0, sizeof(*bar));
    if (bar) {
        if (ShowWindowAsync(window, SW_HIDE)) {
            *bar = (HiddenBar){window, owner, *hidden};
            *hidden = bar;
        } else {
            HeapFree(GetProcessHeap(), 0, bar);
        }
    }
    return TRUE;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    wchar_t *end;
    unsigned long pid = wcstoul(arguments, &end, 10);
    if (!pid || !iswspace(*end))
        return 2;
    HWND topbar = (HWND)(UINT_PTR)wcstoull(end, &end, 10);
    while (iswspace(*end))
        ++end;
    if (!topbar || *end)
        return 2;

    HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, pid);
    if (!process)
        return 1;

    HiddenBar *hidden = NULL;
    do {
        DWORD owner = 0;
        if (!GetWindowThreadProcessId(topbar, &owner) || owner != pid)
            break;
        EnumWindows(hide_taskbar, (LPARAM)&hidden);
    } while (WaitForSingleObject(process, 250) == WAIT_TIMEOUT);

    while (hidden) {
        HiddenBar *bar = hidden;
        DWORD owner = 0;
        if (GetWindowThreadProcessId(bar->window, &owner) &&
            owner == bar->owner && is_taskbar(bar->window))
            ShowWindowAsync(bar->window, SW_SHOWNOACTIVATE);
        hidden = bar->next;
        HeapFree(GetProcessHeap(), 0, bar);
    }
    CloseHandle(process);
    return 0;
}
