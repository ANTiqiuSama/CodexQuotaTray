"""Non-activating, topmost tracking tooltip for the AI tray icons."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import time

import pystray
from pystray._util import win32


WM_USER = 0x0400
TTM_ADDTOOLW = WM_USER + 50
TTM_UPDATETIPTEXTW = WM_USER + 57
TTM_TRACKACTIVATE = WM_USER + 17
TTM_TRACKPOSITION = WM_USER + 18
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOACTIVATE = 0x0010


class HoverIcon(pystray.Icon):
    """Keep an explicit icon ID and opt into application-owned hover UI."""

    # Retain the old installation's ID so Windows preserves its visible pins.
    icon_id = 0
    details_visible = False

    def _message(self, code, flags, **kwargs):
        # pystray 0.19.5 passes hID instead of the actual uID struct member.
        # One icon per HWND lets us use a stable, documented identifier.
        return win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW), hWnd=self._hwnd,
            uID=self.icon_id, uFlags=flags, **kwargs))

    def _show(self):
        self.hover_open = False
        super()._show()
        # Version 4 suppresses the Shell's short tooltip without NIF_SHOWTIP.
        # Set this again after Explorer recreates the notification area.
        self._version4 = bool(self._message(4, 0, uVersion=4))

    def _on_notify(self, wparam, lparam):
        if not getattr(self, "_version4", False):
            return super()._on_notify(wparam, lparam)
        # In v4 the upper word is the icon ID; only the lower word is an event.
        event = lparam & 0xFFFF
        if event in (WM_USER, WM_USER + 1):  # NIN_SELECT / NIN_KEYSELECT
            self.hover_open = False
            self()
        elif event == 0x007B:  # WM_CONTEXTMENU
            self.hover_open = False
            super()._on_notify(0, win32.WM_RBUTTONUP)
        elif event in (WM_USER + 6, WM_USER + 7):  # POPUPOPEN / POPUPCLOSE
            self.hover_open = event == WM_USER + 6


class NotifyIconIdentifier(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("guidItem", ctypes.c_byte * 16)]


class ToolInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("hwnd", wintypes.HWND), ("uId", ctypes.c_size_t),
                ("rect", wintypes.RECT), ("hinst", wintypes.HINSTANCE),
                ("lpszText", wintypes.LPWSTR), ("lParam", wintypes.LPARAM),
                ("lpReserved", ctypes.c_void_p)]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user = ctypes.WinDLL("user32", use_last_error=True)
shell = ctypes.WinDLL("shell32", use_last_error=True)
common = ctypes.WinDLL("comctl32", use_last_error=True)
user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
user.CreateWindowExW.restype = wintypes.HWND
user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user.SendMessageW.restype = ctypes.c_ssize_t
user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user.SetWindowPos.restype = wintypes.BOOL
user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user.DestroyWindow.argtypes = [wintypes.HWND]
user.WindowFromPoint.argtypes = [wintypes.POINT]
user.WindowFromPoint.restype = wintypes.HWND
user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user.GetAncestor.restype = wintypes.HWND
user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user.GetWindow.restype = wintypes.HWND
user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user.GetWindowLongW.restype = wintypes.LONG
user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user.GetAsyncKeyState.argtypes = [ctypes.c_int]
user.GetAsyncKeyState.restype = ctypes.c_short
user.IsWindowVisible.argtypes = [wintypes.HWND]
user.IsWindowVisible.restype = wintypes.BOOL
user.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
user.MonitorFromRect.restype = wintypes.HANDLE
user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
    wintypes.UINT, wintypes.UINT, wintypes.UINT]
user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user.DispatchMessageW.restype = ctypes.c_ssize_t
shell.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NotifyIconIdentifier), ctypes.POINTER(wintypes.RECT)]
shell.Shell_NotifyIconGetRect.restype = ctypes.c_long


def icon_rect(icon) -> tuple[int, int, int, int] | None:
    if not icon.visible or not icon._hwnd:
        return None
    identifier = NotifyIconIdentifier(cbSize=ctypes.sizeof(NotifyIconIdentifier),
        hWnd=icon._hwnd, uID=icon.icon_id)
    rect = wintypes.RECT()
    if shell.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rect)) == 0:
        if rect.right > rect.left and rect.bottom > rect.top:
            return rect.left, rect.top, rect.right, rect.bottom
    return None


def contains(rect, x, y) -> bool:
    return rect[0] <= x < rect[2] and rect[1] <= y < rect[3]


def tooltip_position(anchor, size, work_area, gap=8):
    """Keep the entire popup on the icon's monitor, including negative coords."""
    width, height = size
    left, top, right, bottom = work_area
    x = max(left, min(anchor[2] - width, right - width))
    y = anchor[1] - height - gap
    if y < top:
        y = anchor[3] + gap
    return x, max(top, min(y, bottom - height))


def notification_area_at(point) -> bool:
    # Shell keeps rectangles for hidden/occluded icons too. Do not display a
    # phantom popup when a fullscreen app covers the actual notification area.
    hwnd = user.WindowFromPoint(point)
    root = user.GetAncestor(hwnd, 2) if hwnd else None  # GA_ROOT
    name = ctypes.create_unicode_buffer(256)
    if root:
        user.GetClassNameW(root, name, len(name))
    return name.value in {"Shell_TrayWnd", "Shell_SecondaryTrayWnd",
        "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland"}


def is_window_covered(hwnd, rect) -> bool:
    # WindowFromPoint skips static text controls and transparent surfaces, so
    # hit testing cannot prove that a fullscreen window is behind our popup.
    above = user.GetWindow(hwnd, 3)  # GW_HWNDPREV, actual z-order
    for _ in range(256):
        if not above:
            return False
        if user.IsWindowVisible(above) and not (user.GetWindowLongW(above, -20) & 0x20):
            other = wintypes.RECT()
            if user.GetWindowRect(above, ctypes.byref(other)):
                if (other.left < rect.right and other.right > rect.left
                        and other.top < rect.bottom and other.bottom > rect.top):
                    return True
        above = user.GetWindow(above, 3)
    return False


class TrackingTooltip:
    """All window operations run on the owning thread; focus is never requested."""

    def __init__(self):
        common.InitCommonControls()
        self.owner = user.CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "STATIC", "CodexQuotaTray hover controller", 0x80000000,
            0, 0, 0, 0, None, None, None, None)
        self.hwnd = user.CreateWindowExW(WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            "tooltips_class32", "AI 数据详情", 0x80000000 | 1 | 2 | 0x10 | 0x20,
            0, 0, 0, 0, self.owner, None, None, None)
        if not self.owner or not self.hwnd:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.tool = ToolInfo(cbSize=ctypes.sizeof(ToolInfo), uFlags=0x20 | 0x80,
            hwnd=self.owner, uId=1, lpszText="")  # TRACK | ABSOLUTE
        self.text = ""
        self.visible = False
        if not self.send(TTM_ADDTOOLW, 0, ctypes.addressof(self.tool)):
            self.close()
            raise OSError("无法创建 AI 悬停详情")
        self.send(WM_USER + 24, 0, 520)  # TTM_SETMAXTIPWIDTH enables multiline text

    def send(self, message, wparam=0, lparam=0):
        return user.SendMessageW(self.hwnd, message, wparam, lparam)

    def show(self, text, anchor):
        first_show = not self.visible
        if text != self.text:
            self.buffer = ctypes.create_unicode_buffer(text)
            self.tool.lpszText = ctypes.cast(self.buffer, wintypes.LPWSTR)
            self.send(TTM_UPDATETIPTEXTW, 0, ctypes.addressof(self.tool))
            self.text = text
        if not self.visible:
            position = (anchor[0] & 0xFFFF) | ((anchor[1] & 0xFFFF) << 16)
            self.send(TTM_TRACKPOSITION, 0, position)
            self.send(TTM_TRACKACTIVATE, 1, ctypes.addressof(self.tool))
            self.visible = True
        rect = wintypes.RECT()
        user.GetWindowRect(self.hwnd, ctypes.byref(rect))
        native_anchor = wintypes.RECT(*anchor)
        monitor = user.MonitorFromRect(ctypes.byref(native_anchor), 2)
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        work = info.rcWork
        x, y = tooltip_position(anchor, (rect.right - rect.left, rect.bottom - rect.top),
            (work.left, work.top, work.right, work.bottom))
        if (rect.left, rect.top) != (x, y):
            # Keep the tooltip control's tracking position in sync as well.
            # Merely moving its HWND lets its next timer/text update snap back.
            self.send(TTM_TRACKPOSITION, 0, (x & 0xFFFF) | ((y & 0xFFFF) << 16))
        covered = is_window_covered(self.hwnd, rect)
        if first_show or covered or not user.IsWindowVisible(self.hwnd):
            # Show once; reassert only after actual occlusion. Repeating
            # SWP_SHOWWINDOW every 100ms restarts the tooltip's own lifecycle.
            if not user.SetWindowPos(self.hwnd, wintypes.HWND(-1), x, y, 0, 0,
                    0x0001 | SWP_NOACTIVATE | 0x0040):
                raise ctypes.WinError(ctypes.get_last_error())

    def hide(self):
        if self.visible:
            self.send(TTM_TRACKACTIVATE, 0, ctypes.addressof(self.tool))
            self.visible = False

    @staticmethod
    def pump():
        msg = wintypes.MSG()
        while user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user.TranslateMessage(ctypes.byref(msg))
            user.DispatchMessageW(ctypes.byref(msg))

    def close(self):
        for name in ("hwnd", "owner"):
            handle = getattr(self, name, None)
            if handle:
                user.DestroyWindow(handle)
                setattr(self, name, None)


class HoverDetails:
    def __init__(self, icons, text_for, stop_event):
        self.icons, self.text_for, self.stop_event = icons, text_for, stop_event
        self.candidate = None
        self.entered_at = 0.0

    def update(self, popup, provider, anchor, now, dwell=0.4):
        if provider != self.candidate:
            popup.hide()
            self.candidate, self.entered_at = provider, now
        if provider is None:
            popup.hide()
        elif now - self.entered_at >= dwell:
            popup.show(self.text_for(provider), anchor)

    def target_at(self, point):
        for key, icon in self.icons.items():
            # A Shell refresh can send POPUPCLOSE while the mouse is still on
            # the icon. Once opened, keep our popup until the pointer leaves.
            if (getattr(icon, "_version4", False) and not icon.hover_open
                    and self.candidate != key):
                continue
            rect = icon_rect(icon)
            if rect and contains(rect, point.x, point.y):
                return key, rect
        return None, None

    def run(self):
        popup = None
        try:
            popup = TrackingTooltip()
            while not self.stop_event.wait(0.1):
                popup.pump()
                point = wintypes.POINT()
                provider, anchor = None, None
                if (user.GetCursorPos(ctypes.byref(point)) and notification_area_at(point)
                        and not any(user.GetAsyncKeyState(key) & 0x8000 for key in (1, 2))):
                    provider, anchor = self.target_at(point)
                for key, icon in self.icons.items():
                    icon.details_visible = key == provider
                shell_dwell = provider and getattr(self.icons[provider], "_version4", False)
                self.update(popup, provider, anchor, time.monotonic(), dwell=0 if shell_dwell else 0.4)
        except Exception:
            logging.getLogger(__name__).exception("AI hover details stopped")
            # Restore the Shell tooltip if the independent popup cannot run.
            for icon in self.icons.values():
                icon._message(1, win32.NIF_TIP | 0x80, szTip=icon.title)
        finally:
            for icon in self.icons.values():
                icon.details_visible = False
            if popup:
                popup.close()
