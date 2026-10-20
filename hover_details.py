"""Hover UI for the AI tray icons: the agent panel, with the native tooltip
as a fallback when the layered panel cannot be created."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import time

import pystray
from pystray._util import win32

from detail_card import MARGIN
import agent_panel


WM_USER = 0x0400
TTM_ADDTOOLW = WM_USER + 50
TTM_UPDATETIPTEXTW = WM_USER + 57
TTM_TRACKACTIVATE = WM_USER + 17
TTM_TRACKPOSITION = WM_USER + 18
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
SWP_NOACTIVATE = 0x0010
SW_HIDE = 0
ULW_ALPHA = 2


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
        event = lparam & 0xFFFF if getattr(self, "_version4", False) else lparam
        action = getattr(self, "settings_action", None)
        if action and event in (0x007B, win32.WM_RBUTTONUP):
            self.hover_open = False
            self.suppress_hover = True
            action()
            return
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


class BlendFunction(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


user = ctypes.WinDLL("user32", use_last_error=True)
gdi = ctypes.WinDLL("gdi32", use_last_error=True)
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
user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user.GetDC.argtypes = [wintypes.HWND]
user.GetDC.restype = wintypes.HDC
user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
    ctypes.POINTER(BlendFunction), wintypes.DWORD]
user.UpdateLayeredWindow.restype = wintypes.BOOL
gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi.CreateCompatibleDC.restype = wintypes.HDC
gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi.CreateDIBSection.restype = wintypes.HBITMAP
gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi.SelectObject.restype = wintypes.HGDIOBJ
gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi.DeleteDC.argtypes = [wintypes.HDC]
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


def enable_thread_dpi_awareness() -> bool:
    """Per-monitor v2 for the hover thread only, so the card is drawn at the
    monitor's real DPI instead of being bitmap-stretched (blurry) by DWM. All
    coordinates this thread reads (cursor, icon rect, monitor) become physical
    together. CODEX_QUOTA_LEGACY_DPI=1 keeps the previous behaviour."""
    if os.environ.get("CODEX_QUOTA_LEGACY_DPI") == "1":
        return False
    try:
        function = user.SetThreadDpiAwarenessContext
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_void_p
        return bool(function(ctypes.c_void_p(-4)))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        return False


def monitor_scale(monitor) -> float:
    try:
        shcore = ctypes.WinDLL("shcore")
        x, y = wintypes.UINT(), wintypes.UINT()
        if shcore.GetDpiForMonitor(wintypes.HANDLE(monitor), 0, ctypes.byref(x), ctypes.byref(y)) == 0 and x.value:
            return x.value / 96
    except (AttributeError, OSError):
        pass
    return 1.0


def content_text(content) -> str:
    return content if isinstance(content, str) else content.as_text()


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
        text = content_text(text)
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


def work_area(anchor):
    """(monitor, work rectangle) of the monitor holding the tray icon."""
    native_anchor = wintypes.RECT(*anchor)
    monitor = user.MonitorFromRect(ctypes.byref(native_anchor), 2)
    info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
    if not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    work = info.rcWork
    return monitor, (work.left, work.top, work.right, work.bottom)


def present(hwnd, image, x, y, alpha=255):
    """Hand a straight-alpha PIL image to a layered window (premultiplied BGRA)."""
    width, height = image.size
    data = image.convert("RGBa").tobytes("raw", "BGRa")
    header = BitmapInfoHeader(biSize=ctypes.sizeof(BitmapInfoHeader), biWidth=width,
        biHeight=-height, biPlanes=1, biBitCount=32, biCompression=0)
    screen = user.GetDC(None)
    memory = gdi.CreateCompatibleDC(screen)
    bits = ctypes.c_void_p()
    bitmap = gdi.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    try:
        if not bitmap or not bits:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.memmove(bits, data, len(data))
        previous = gdi.SelectObject(memory, bitmap)
        blend = BlendFunction(0, 0, alpha, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        ok = user.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wintypes.POINT(x, y)),
            ctypes.byref(wintypes.SIZE(width, height)), memory, ctypes.byref(wintypes.POINT(0, 0)),
            0, ctypes.byref(blend), ULW_ALPHA)
        gdi.SelectObject(memory, previous)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if bitmap:
            gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory)
        user.ReleaseDC(None, screen)


class AgentPanelWindow:
    """Interactive bubble panel. Layered and topmost like the card, but it
    takes the pointer (no WS_EX_TRANSPARENT) so bubbles can be dragged; it is
    still never activated, so focus stays in the user's window."""

    OPEN_SECONDS, CLOSE_SECONDS = 0.2, 0.14

    def __init__(self):
        self.hwnd = user.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            "STATIC", "AI Agent 任务", 0x80000000 | 0x0100, 0, 0, 0, 0, None, None, None, None)  # WS_POPUP | SS_NOTIFY
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.scene = agent_panel.Scene()
        self.provider = None
        self.anchor = None
        self.rect = None
        self.scale = 1.0
        self.work = None
        self.opened_at = self.closing_at = None
        self.shown = False
        self.checked_at = 0.0

    @property
    def active(self) -> bool:
        return self.provider is not None

    def open(self, provider, model, anchor, now):
        monitor, self.work = work_area(anchor)
        self.scale = monitor_scale(monitor)
        if self.provider is None or self.closing_at is not None:
            self.scene = agent_panel.Scene()
            self.opened_at, self.closing_at = now, None
        self.provider, self.anchor = provider, anchor
        self.scene.set_model(model, now)

    def refresh(self, model, now):
        self.scene.set_model(model, now)

    def contains(self, point, slack=6) -> bool:
        rect = self.rect
        return bool(rect) and rect[0] - slack <= point.x < rect[2] + slack and rect[1] - slack <= point.y < rect[3] + slack

    def local(self, point):
        if not self.rect:
            return None
        origin = MARGIN * self.scale
        return ((point.x - self.rect[0] - origin) / self.scale, (point.y - self.rect[1] - origin) / self.scale)

    def close(self, now):
        if self.provider is not None and self.closing_at is None:
            self.closing_at = now

    def frame(self, point, left, right, now):
        """Advance and present one frame; returns the scene's input events."""
        inside = point is not None and self.contains(point, 0)
        events = self.scene.step(now, self.local(point) if inside or self.scene.dragging else None,
                                 left and (inside or bool(self.scene.press)), right and inside)
        image = self.scene.render(self.scale)
        margin = round(MARGIN * self.scale)
        x, y = tooltip_position(self.anchor, image.size, self.work, gap=round(6 * self.scale) - margin)
        if self.closing_at is not None:
            progress = min(1.0, (now - self.closing_at) / self.CLOSE_SECONDS)
            alpha, lift = 1 - progress, -4 * progress
            if progress >= 1:
                self.hide()
                return []
        else:
            progress = agent_panel.ease_out_cubic((now - self.opened_at) / self.OPEN_SECONDS)
            alpha, lift = progress, 10 * (1 - progress)
        y += round(lift * self.scale)
        present(self.hwnd, image, x, y, round(255 * alpha))
        self.rect = (x, y, x + image.width, y + image.height)
        if not self.shown or now - self.checked_at > 0.5:
            # Z-order walk is cheap but not free; twice a second is plenty.
            self.checked_at = now
            if not self.shown or is_window_covered(self.hwnd, wintypes.RECT(*self.rect)):
                user.SetWindowPos(self.hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | SWP_NOACTIVATE | 0x0040)
                self.shown = True
        return events

    def hide(self):
        user.ShowWindow(self.hwnd, SW_HIDE)
        self.provider = self.rect = self.closing_at = None
        self.shown = False

    def destroy(self):
        if self.hwnd:
            user.DestroyWindow(self.hwnd)
            self.hwnd = None


class HoverDetails:
    """Hover a tray icon for its agent panel; it stays while the pointer is on
    it. Without a panel (no `agents`, or a GDI failure) the native tooltip
    shows `text_for(provider)`."""

    PANEL_GRACE = 0.45          # Time to travel from the icon to the panel.
    PANEL_REFRESH = 0.5

    def __init__(self, icons, text_for, stop_event, agents=None):
        self.icons, self.text_for, self.stop_event = icons, text_for, stop_event
        self.agents = agents     # object with panel(provider), merge(a, b), split(key)
        self.candidate = None
        self.entered_at = 0.0
        self.panel_seen = 0.0
        self.panel_built = 0.0

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
            rect = icon_rect(icon)
            if getattr(icon, "suppress_hover", False):
                if not rect or not contains(rect, point.x, point.y):
                    icon.suppress_hover = False
                continue
            # A Shell refresh can send POPUPCLOSE while the mouse is still on
            # the icon. Once opened, keep our popup until the pointer leaves.
            if (getattr(icon, "_version4", False) and not icon.hover_open
                    and self.candidate != key):
                continue
            if rect and contains(rect, point.x, point.y):
                return key, rect
        return None, None

    def drive_panel(self, panel, point, provider, anchor, left, right, now) -> None:
        """One frame of the open panel: hand input to the scene, act on drops."""
        if provider:
            # Remember the icon actually under the pointer. After switching icons
            # the Shell sends POPUPCLOSE a few seconds later; with the old
            # candidate the new icon stopped counting as hovered and the panel
            # closed while the pointer was still on it.
            self.candidate = provider
        if provider and provider != panel.provider and not panel.scene.dragging:
            model = self.agents.panel(provider)
            if model is None:           # e.g. Claude without a subscription: tooltip only
                panel.close(now)
                return
            panel.open(provider, model, anchor, now)
        over = panel.contains(point) or provider == panel.provider
        if over or panel.scene.dragging:
            self.panel_seen = now
        elif now - self.panel_seen > self.PANEL_GRACE or (left or right) and not panel.contains(point, 40):
            panel.close(now)
        if (now - self.panel_built >= self.PANEL_REFRESH and panel.closing_at is None
                and not panel.scene.press and not panel.scene.dragging):
            model = self.agents.panel(panel.provider)
            if model is None:
                panel.close(now)
                return
            panel.refresh(model, now)
            self.panel_built = now
        for event in panel.frame(point, left, right, now):
            if event[0] == "settings":
                panel.hide()
                self.candidate = None
                self.agents.open_settings()
                return
            elif event[0] == "merge":
                merged = self.agents.merge(event[1], event[2])
                if merged:
                    panel.scene.absorb(event[1], event[2], merged, now)
            elif event[0] == "split":
                members = self.agents.split(event[1])
                if members:
                    panel.scene.burst(event[1], members, now)
            model = self.agents.panel(panel.provider)
            if model is not None:
                panel.refresh(model, now)

    def run(self):
        tooltip = panel = None
        try:
            enable_thread_dpi_awareness()
            if self.agents is not None:
                try:
                    panel = AgentPanelWindow()
                except Exception:
                    logging.getLogger(__name__).exception("agent panel unavailable, using tooltip")
            if panel is None:
                tooltip = TrackingTooltip()
            while not self.stop_event.wait(0.016 if panel and panel.active else 0.1):
                TrackingTooltip.pump()
                requested = getattr(self.agents, "settings_requested", None)
                if requested is not None and requested.is_set():
                    requested.clear()
                    self.candidate = None
                    if panel:
                        panel.hide()
                    if tooltip:
                        tooltip.hide()
                point = wintypes.POINT()
                provider, anchor = None, None
                have_point = user.GetCursorPos(ctypes.byref(point))
                if have_point:
                    for icon in self.icons.values():
                        if getattr(icon, "suppress_hover", False):
                            rect = icon_rect(icon)
                            if not rect or not contains(rect, point.x, point.y):
                                icon.suppress_hover = False
                left, right = (bool(user.GetAsyncKeyState(key) & 0x8000) for key in (1, 2))
                if have_point and notification_area_at(point) and not (left or right):
                    provider, anchor = self.target_at(point)
                now = time.monotonic()
                if panel is not None:
                    try:
                        if panel.active:
                            self.drive_panel(panel, point, provider, anchor, left, right, now)
                        else:
                            if provider != self.candidate:
                                self.candidate, self.entered_at = provider, now
                            shell_dwell = provider and getattr(self.icons[provider], "_version4", False)
                            model = (self.agents.panel(provider)
                                     if provider and now - self.entered_at >= (0 if shell_dwell else 0.4) else None)
                            if model is not None:
                                panel.open(provider, model, anchor, now)
                                self.panel_seen = self.panel_built = now
                            elif provider and now - self.entered_at >= (0 if shell_dwell else 0.4):
                                # Sources without a quota panel keep the native tooltip.
                                tooltip = tooltip or TrackingTooltip()
                                tooltip.show(self.text_for(provider), anchor)
                        if tooltip and (panel.active or provider is None):
                            tooltip.hide()
                        shown = panel.provider if panel.active else (provider if tooltip and tooltip.visible else None)
                    except Exception:
                        # A rendering/GDI failure downgrades to the native tooltip
                        # instead of losing hover details for the whole session.
                        logging.getLogger(__name__).exception("agent panel failed, using tooltip")
                        panel.hide()
                        panel.destroy()
                        panel, tooltip = None, TrackingTooltip()
                        shown = None
                    if panel is not None:
                        for key, icon in self.icons.items():
                            icon.details_visible = key == shown
                        continue
                for key, icon in self.icons.items():
                    icon.details_visible = key == provider
                shell_dwell = provider and getattr(self.icons[provider], "_version4", False)
                self.update(tooltip, provider, anchor, now, dwell=0 if shell_dwell else 0.4)
        except Exception:
            logging.getLogger(__name__).exception("AI hover details stopped")
            # Restore the Shell tooltip if the independent popup cannot run.
            for icon in self.icons.values():
                icon._message(1, win32.NIF_TIP | 0x80, szTip=icon.title)
        finally:
            for icon in self.icons.values():
                icon.details_visible = False
            if tooltip:
                tooltip.close()
            if panel:
                panel.destroy()
