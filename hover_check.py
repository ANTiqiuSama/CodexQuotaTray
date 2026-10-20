"""Bounded native-window smoke check of the agent panel; never sends input
or requests focus."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time

from hover_details import (AgentPanelWindow, MonitorInfo, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST, WS_EX_TRANSPARENT, enable_thread_dpi_awareness, is_window_covered, user)


def check_hover_runtime(model, destination: Path) -> int:
    """Open the real panel over a fullscreen topmost window, animate it for a
    few seconds and confirm it stays on top, inside the work area, steady
    after its opening animation, and never takes the foreground."""
    enable_thread_dpi_awareness()
    user.GetForegroundWindow.restype = wintypes.HWND
    foreground = user.GetForegroundWindow()
    panel = AgentPanelWindow()
    native_setpos, native_showwindow = user.SetWindowPos, user.ShowWindow
    calls = {"showwindow": 0, "hide": 0}

    def tracked_setpos(hwnd, after, x, y, width, height, flags):
        if hwnd == panel.hwnd and flags & 0x0040:
            calls["showwindow"] += 1
        return native_setpos(hwnd, after, x, y, width, height, flags)

    def tracked_showwindow(hwnd, command):
        if hwnd == panel.hwnd and command == 0:
            calls["hide"] += 1
        return native_showwindow(hwnd, command)

    user.SetWindowPos, user.ShowWindow = tracked_setpos, tracked_showwindow
    fixture = None
    report = {"pid": os.getpid(), "foreground_before": foreground, "bubbles": len(model.bubbles)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        primary = wintypes.RECT(0, 0, 1, 1)
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if not user.GetMonitorInfoW(user.MonitorFromRect(ctypes.byref(primary), 2), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        screen, work = info.rcMonitor, info.rcWork
        fixture = user.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "STATIC", "AI 任务面板：全屏覆盖测试（数秒后自动关闭，不改变键盘焦点）",
            0x80000000 | 0x0201, screen.left, screen.top,
            screen.right - screen.left, screen.bottom - screen.top, None, None, None, None)
        if not fixture:
            raise ctypes.WinError(ctypes.get_last_error())
        user.SetWindowPos(fixture, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
        anchor = (work.right - 300, work.bottom + 8, work.right - 276, work.bottom + 32)
        started = time.monotonic()
        panel.open(model.provider, model, anchor, started)
        frames, rects = 0, []
        while time.monotonic() - started < 4:
            now = time.monotonic()
            if 1.0 <= now - started < 1.02:
                # Simulate a fullscreen app reasserting its z-order later on.
                user.SetWindowPos(fixture, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            panel.frame(None, False, False, now)
            frames += 1
            if now - started > 0.4:
                rect = wintypes.RECT()
                user.GetWindowRect(panel.hwnd, ctypes.byref(rect))
                rects.append((rect.left, rect.top, rect.right, rect.bottom))
            time.sleep(0.016)
        rect = wintypes.RECT(*rects[-1])
        style = user.GetWindowLongW(panel.hwnd, -20)
        elapsed = time.monotonic() - started
        report.update({
            "panel_rect": list(rects[-1]), "work_area": [work.left, work.top, work.right, work.bottom],
            "frames": frames, "fps": round(frames / elapsed, 1),
            "visible": bool(user.IsWindowVisible(panel.hwnd)),
            "topmost": bool(style & WS_EX_TOPMOST), "layered": bool(style & WS_EX_LAYERED),
            "noactivate": bool(style & WS_EX_NOACTIVATE), "accepts_pointer": not style & WS_EX_TRANSPARENT,
            "above_fullscreen_after_zorder_change": not is_window_covered(panel.hwnd, rect),
            "within_work_area": work.left <= rect.left < rect.right <= work.right and work.top <= rect.top < rect.bottom <= work.bottom,
            "steady_after_opening": len(set(rects)) == 1,
            "no_repeated_show_or_hide": calls["showwindow"] <= 3 and calls["hide"] == 0,
            "native_calls": dict(calls),
            "foreground_preserved": user.GetForegroundWindow() == foreground,
        })
    finally:
        panel.hide()
        panel.destroy()
        user.SetWindowPos, user.ShowWindow = native_setpos, native_showwindow
        if fixture:
            user.DestroyWindow(fixture)
    fields = ("visible", "topmost", "layered", "noactivate", "accepts_pointer", "above_fullscreen_after_zorder_change",
              "within_work_area", "steady_after_opening", "no_repeated_show_or_hide", "foreground_preserved")
    report["passed"] = all(report.get(key, False) for key in fields)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1
