"""Bounded native-window smoke check; never sends input or requests focus."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time

from hover_details import (MonitorInfo, TrackingTooltip, WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW, WS_EX_TOPMOST, TTM_TRACKACTIVATE, is_window_covered, user)


def check_hover_runtime(text: str, destination: Path) -> int:
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongW.restype = wintypes.LONG
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    foreground = user.GetForegroundWindow()
    popup = TrackingTooltip()
    native_send = popup.send
    native_setpos = user.SetWindowPos
    calls = {"activate": 0, "deactivate": 0, "showwindow": 0}
    def tracked_send(message, wparam=0, lparam=0):
        if message == TTM_TRACKACTIVATE:
            calls["activate" if wparam else "deactivate"] += 1
        return native_send(message, wparam, lparam)
    def tracked_setpos(hwnd, after, x, y, width, height, flags):
        if hwnd == popup.hwnd and flags & 0x0040:
            calls["showwindow"] += 1
        return native_setpos(hwnd, after, x, y, width, height, flags)
    popup.send, user.SetWindowPos = tracked_send, tracked_setpos
    fixture = None
    report = {"pid": os.getpid(), "foreground_before": foreground}
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        primary = wintypes.RECT(0, 0, 1, 1)
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if not user.GetMonitorInfoW(user.MonitorFromRect(ctypes.byref(primary), 2), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        screen, work = info.rcMonitor, info.rcWork
        fixture = user.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "STATIC", "AI 数据详情：全屏覆盖测试（数秒后自动关闭，不改变键盘焦点）",
            0x80000000 | 0x0201, screen.left, screen.top,
            screen.right - screen.left, screen.bottom - screen.top,
            None, None, None, None)
        if not fixture:
            raise ctypes.WinError(ctypes.get_last_error())
        user.SetWindowPos(fixture, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
        anchor = (work.right - 300, work.bottom + 8, work.right - 276, work.bottom + 32)
        popup.show(text + "\n连续刷新验证：0", anchor)
        popup.pump()
        # Simulate a fullscreen/topmost application's later z-order change.
        user.SetWindowPos(fixture, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        popup.show(text + "\n连续刷新验证：0", anchor)
        popup.pump()
        rect = wintypes.RECT()
        user.GetWindowRect(popup.hwnd, ctypes.byref(rect))
        style = user.GetWindowLongW(popup.hwnd, -20)
        report.update({"tooltip_hwnd": popup.hwnd, "fixture_hwnd": fixture,
            "text": text, "text_length": len(text),
            "tooltip_rect": [rect.left, rect.top, rect.right, rect.bottom],
            "fullscreen_fixture_rect": [screen.left, screen.top, screen.right, screen.bottom],
            "work_area": [work.left, work.top, work.right, work.bottom],
            "visible": bool(user.IsWindowVisible(popup.hwnd)),
            "topmost": bool(style & WS_EX_TOPMOST),
            "noactivate": bool(style & WS_EX_NOACTIVATE),
            "above_fullscreen_after_zorder_change": not is_window_covered(popup.hwnd, rect),
            "foreground_preserved_on_show": user.GetForegroundWindow() == foreground,
            "within_work_area": work.left <= rect.left < rect.right <= work.right
                and work.top <= rect.top < rect.bottom <= work.bottom})
        ready = destination.with_suffix(".ready.json")
        ready.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        started = time.monotonic()
        deadline = started + 4
        samples = []
        while time.monotonic() < deadline:
            popup.pump()
            popup.show(text + f"\n连续刷新验证：{int(time.monotonic()-started)}", anchor)
            sample_rect = wintypes.RECT()
            user.GetWindowRect(popup.hwnd, ctypes.byref(sample_rect))
            samples.append({"visible": bool(user.IsWindowVisible(popup.hwnd)),
                "rect": [sample_rect.left, sample_rect.top, sample_rect.right, sample_rect.bottom]})
            time.sleep(0.05)
        report["continuous_update_samples"] = len(samples)
        report["continuous_update_stable"] = bool(samples) and all(
            item["visible"] and item["rect"] == samples[0]["rect"] for item in samples)
        report["native_calls"] = dict(calls)
        report["no_repeated_show_or_hide"] = calls["activate"] == 1 and calls["deactivate"] == 0 and calls["showwindow"] <= 2
        report["foreground_preserved_until_close"] = user.GetForegroundWindow() == foreground
    finally:
        popup.close()
        user.SetWindowPos = native_setpos
        if fixture:
            user.DestroyWindow(fixture)
    fields = ("visible", "topmost", "noactivate", "above_fullscreen_after_zorder_change",
              "foreground_preserved_on_show", "within_work_area", "continuous_update_stable", "no_repeated_show_or_hide")
    report["passed"] = all(report.get(key, False) for key in fields)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1
