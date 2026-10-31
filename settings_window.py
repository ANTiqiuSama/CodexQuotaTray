"""User-opened settings window; secrets stay in the local data directory.

Five pages:
* 概览     live source cards in the tray's order, written like the hover panel,
           the tray-icon check and the last half hour of output;
* 来源     which sources are shown, MiMo's console login / manual plan, and the
           local records each reader uses;
* 模型智商 the panel banner's AI Stupid Level scores and Grok's LiveBench column;
* 任务     herdr and the optional task-naming API;
* 常规     start with Windows, how often each source is read, tools, exit.

Switches and form fields are staged: the footer lists what is not saved yet and
保存 applies it (source switches restart the tray). Buttons such as 登录 or
立即读取 act at once and say so next to them.
"""
from __future__ import annotations

import json
import ctypes
import logging
import os
import queue
import re
import threading
import time
from urllib.parse import urlsplit

import agent_tasks
import settings_view as view


def load_summary() -> dict:
    try:
        stored = json.loads(agent_tasks.SUMMARY_CONFIG.read_text(encoding="utf-8"))
        return {**{name: str(stored.get(name) or "") for name in ("url", "key", "model")},
                "enabled": stored.get("enabled", True)}
    except (OSError, ValueError, AttributeError):
        return {"url": "", "key": "", "model": "", "enabled": False}


def save_summary(config: dict, enabled: bool) -> None:
    values = {name: str(config.get(name) or "").strip() for name in ("url", "key", "model")}
    if enabled:
        address = urlsplit(values["url"])
        if address.scheme not in ("http", "https") or not address.hostname or not values["model"]:
            raise ValueError("请填写有效的 API 地址与模型名称")
        if agent_tasks.is_mimo_url(values["url"]) and not values["key"]:
            raise ValueError("MiMo 需要填写 API Key")
    path = agent_tasks.SUMMARY_CONFIG.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**values, "enabled": enabled}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


PAGES = (("overview", "概览"), ("sources", "来源"), ("iq", "模型智商"), ("tasks", "任务"), ("general", "常规"))
# Older page names (the panel, the tray menu) land on the page that now holds them.
PAGE_ALIASES = {"mimo": "sources", "agents": "tasks", "naming": "tasks"}
PROVIDER_NAMES = {"codex": "Codex", "grok": "Grok", "gemini": "Gemini", "claude": "Claude Code", "mimo": "Xiaomi MiMo"}
NOTICE_SECONDS = 6
_LATIN = re.compile(r"[A-Za-z0-9_./:@%+-]")


def apply_dark_titlebar(root) -> bool:
    """Use the native Windows caption and keep its minimize/maximize controls."""
    if os.name != "nt":
        return False
    try:
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        child = root.winfo_id()
        hwnd = user32.GetParent(child) or child
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        enabled = ctypes.c_int(1)
        # 20 is current Windows; 19 was used by earlier Windows 10 builds.
        applied = any(dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(enabled), ctypes.sizeof(enabled)) == 0
                      for attr in (20, 19))
        if applied:
            # Windows 11 supports explicit caption/text colours; Windows 10
            # ignores these attributes while retaining immersive dark mode.
            for attr, color in ((35, 0x00181A1B), (36, 0x00E6EEF0)):
                value = ctypes.c_uint32(color)
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        return applied
    except (OSError, AttributeError, ValueError):
        return False


def wrap_text(text: str, measure, width: int) -> str:
    """Line breaks for mixed Chinese / Latin text. Tk wraps only at spaces, so a
    space inside Chinese ("每 3 小时") ends the line there and pushes a whole
    clause down. Break by character instead; a Latin word stays whole when it
    can move to the next line."""
    lines = []
    for paragraph in (text or "").split("\n"):
        line = ""
        for char in paragraph:
            if not line or measure(line + char) <= width:
                line += char
                continue
            cut = len(line)
            if _LATIN.match(char):
                while cut > 0 and _LATIN.match(line[cut - 1]):
                    cut -= 1
                if cut < len(line) * .5:          # one very long word: break it
                    cut = len(line)
            lines.append(line[:cut].rstrip())
            line = (line[cut:] + char).lstrip()
        lines.append(line)
    return "\n".join(lines)


def letter_of(provider: str) -> str:
    import providers
    spec = providers.BY_KEY.get(provider)
    return spec.letter if spec else "?"


def in_tray_order(keys) -> list[str]:
    import providers
    return providers.tray_positions(keys)


def card_rows(model, snapshot, now: float) -> tuple:
    """The panel's header columns as card rows; the window name joins a
    countdown title so the card still says which window it is."""
    quota = getattr(model, "quota", None)
    if not quota:
        return view.quota_rows(snapshot, now)
    names = []
    if snapshot is not None and not (snapshot.pools and len(snapshot.pools) > 1):
        names = [view.window_name(snapshot.window_minutes), view.window_name(snapshot.secondary_window_minutes)]
    rows = []
    for slot, item in enumerate(quota[:3]):
        title, value = item[0], item[1]
        caption = item[2] if len(item) > 2 else ""
        tone = model.tone_of(slot) if hasattr(model, "tone_of") else ""
        figure = model.figure_of(slot) if hasattr(model, "figure_of") else None
        if not caption and slot < len(names) and title != names[slot]:
            caption = names[slot]
        rows.append((title, value, caption, tone, "".join(figure) if figure else None))
    return tuple(rows)


def build_cards(app, now: float | None = None) -> list[tuple[view.SourceCard, object]]:
    """(card, current tray icon image) for every shown source, in the tray's order."""
    now = now or time.time()
    result = []
    performance = (getattr(app, "performance", None) or {}).get("providers", {})
    scheduler = getattr(app, "scheduler", None)
    for provider in in_tray_order(app.snapshots):
        snapshot = app.snapshots[provider]
        data = performance.get(provider, {})
        try:
            model = app.panel(provider)
        except Exception:
            model = None
        if model is not None:
            status, tone, forecast, forecast_tone = model.status, model.status_tone, model.forecast, model.forecast_tone
        else:
            status, tone, forecast, forecast_tone = ("读取异常" if snapshot is not None and snapshot.error else "空闲"), "idle", "", "unknown"
        refresh = ""
        if scheduler is not None and hasattr(scheduler, "plan"):
            refresh = view.refresh_text(*scheduler.plan(provider, now))
        card = view.SourceCard(provider, letter_of(provider), PROVIDER_NAMES.get(provider, provider), status, tone,
                               card_rows(model, snapshot, now),
                               None if provider == "gemini" else data.get("display_tps"), float(data.get("output_tps_60") or 0.0),
                               int(data.get("active_sessions") or 0), forecast, forecast_tone,
                               (snapshot.error or "") if snapshot is not None else "", refresh)
        icon = getattr((getattr(app, "icons", None) or {}).get(provider), "icon", None)
        result.append((card, icon))
    return result


def interval_text(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds} 秒" if seconds < 60 else (f"{seconds // 60} 分钟" if seconds % 60 == 0 else f"{seconds / 60:.1f} 分钟")


class SettingsController:
    """Keep Tk on one UI thread; tray callbacks only enqueue commands."""
    def __init__(self, app):
        self.app = app
        self.commands = queue.Queue()
        self.thread = None
        self.lock = threading.Lock()

    def show(self, page=None):
        with self.lock:
            self.commands.put(("show", page))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="settings-ui", daemon=True)
                self.thread.start()

    def close(self):
        self.commands.put(("close",))

    def _run(self):
        SettingsWindow(self.app, self.commands).run()


class SettingsWindow:
    """Dark, panel-styled settings: a sidebar, five pages and a save bar."""

    def __init__(self, app, commands):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import messagebox
        from PIL import ImageTk
        self.tk, self.messagebox, self.ImageTk = tk, messagebox, ImageTk
        self.app, self.commands = app, commands
        root = self.root = tk.Tk()
        root.withdraw()
        root.title("CodexQuotaTray 设置")
        root.configure(bg=view.BG)
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        S = self.S = lambda value: round(value * self.scale)  # noqa: E731
        root.geometry(f"{S(1040)}x{S(740)}")
        root.minsize(S(820), S(600))
        root.protocol("WM_DELETE_WINDOW", self.close)
        families = set(tkfont.families(root))
        self.family = next((name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "Noto Sans CJK SC", "Segoe UI")
                            if name in families), "TkDefaultFont")
        self.images = {}              # keep PhotoImage references alive
        self.page = "overview"
        self.hover_card = None
        self.vars = {}
        self.origins = {}
        self.notice = ("", 0.0)
        self.iq_selected: list[str] = []
        self._resize_after = None
        self._resize_size = None
        self._hover_after = None
        self._painted_hover = None
        self._card_models = {}
        self._layout()
        root.bind("<Configure>", self._on_resize, add="+")
        root.bind("<Map>", self._on_map, add="+")

    def _on_map(self, event):
        if event.widget is self.root:
            apply_dark_titlebar(self.root)

    def _on_resize(self, event):
        if event.widget is not self.root:
            return
        size = (event.width, event.height)
        if size == self._resize_size:
            return
        self._resize_size = size
        if self._resize_after is not None:
            self.root.after_cancel(self._resize_after)
        self._resize_after = self.root.after(120, self._finish_resize)

    def _finish_resize(self):
        self._resize_after = None
        if self.root.state() not in ("withdrawn", "iconic"):
            self.refresh_live()

    # ----- small widgets -------------------------------------------------------------
    def font(self, size=10, weight="normal"):
        return (self.family, size, weight)

    def label(self, parent, text="", size=10, color=view.INK, weight="normal", **kw):
        return self.tk.Label(parent, text=text, bg=kw.pop("bg", parent["bg"]), fg=color, font=self.font(size, weight),
                             justify="left", anchor="w", **kw)

    def para(self, parent, text="", size=9, color=view.INK_3, **kw):
        """A paragraph that wraps to the width it is given (pack it with fill="x").
        Change its text with set_text so it is wrapped again."""
        import tkinter.font as tkfont
        widget = self.label(parent, text, size, color, **kw)
        widget.raw, widget.wrapped_at, widget.wrap_after = text, None, None
        face = tkfont.Font(root=self.root, font=widget.cget("font"))
        widget.measure = face.measure

        def apply_wrap(width):
            widget.wrap_after = None
            if self._resize_after is not None:
                widget.wrap_after = widget.after(120, lambda: apply_wrap(width))
                return
            if widget.wrapped_at != (width, widget.raw):
                widget.wrapped_at = (width, widget.raw)
                widget.configure(text=wrap_text(widget.raw, widget.measure, width))

        def wrap(event):
            width = max(self.S(160), event.width - self.S(4))
            if widget.wrap_after is not None:
                widget.after_cancel(widget.wrap_after)
            widget.wrap_after = widget.after(80, lambda: apply_wrap(width))
        widget.bind("<Configure>", wrap, add="+")
        return widget

    @staticmethod
    def set_text(widget, text: str, **kw) -> None:
        """New text for a paragraph (wrapped to its current width) or a plain label."""
        if hasattr(widget, "raw"):
            if widget.raw != text:
                widget.raw = text
                width = (widget.wrapped_at or (0,))[0] or widget.winfo_width()
                widget.wrapped_at = (width, text)
                kw["text"] = wrap_text(text, widget.measure, width) if width > 1 else text
        elif widget.cget("text") != text:
            kw["text"] = text
        if kw:
            widget.configure(**kw)

    def button(self, parent, text, command, kind="ghost"):
        tk = self.tk
        colors = {"primary": (view.GREEN, "#93d6b7", "#1f1e1c"), "ghost": (view.SURFACE_HI, "#3d3c36", view.INK),
                  "danger": ("#3a2524", "#4a2b29", "#ef8a7f"), "off": (view.SURFACE, view.SURFACE, view.INK_3)}
        widget = tk.Label(parent, text=text, font=self.font(10, "bold" if kind == "primary" else "normal"),
                          padx=self.S(14), pady=self.S(6), cursor="hand2")
        widget.kind, widget.enabled = kind, True

        def paint(hover=False):
            base, lit, ink = colors[widget.kind if widget.enabled else "off"]
            widget.configure(bg=lit if hover and widget.enabled else base, fg=ink, cursor="hand2" if widget.enabled else "arrow")

        def press(_):
            if widget.enabled:
                command()

        def set_enabled(on):
            if widget.enabled != on:
                widget.enabled = on
                paint()

        widget.set_enabled = set_enabled
        widget.bind("<Enter>", lambda _: paint(True))
        widget.bind("<Leave>", lambda _: paint(False))
        widget.bind("<Button-1>", press)
        paint()
        return widget

    def link(self, parent, text, command, color=view.GREEN, size=9):
        widget = self.label(parent, text, size, color, cursor="hand2")
        widget.bind("<Button-1>", lambda _: command())
        return widget

    def switch(self, parent, variable, command=None):
        tk, S = self.tk, self.S
        canvas = tk.Canvas(parent, width=S(40), height=S(22), bg=parent["bg"], highlightthickness=0, cursor="hand2")

        def draw(*_):
            canvas.delete("all")
            on = bool(variable.get())
            fill = view.GREEN if on else "#4a4842"
            r = S(11)
            canvas.create_oval(0, 0, 2 * r, 2 * r, fill=fill, outline=fill)
            canvas.create_oval(S(40) - 2 * r, 0, S(40), 2 * r, fill=fill, outline=fill)
            canvas.create_rectangle(r, 0, S(40) - r, 2 * r, fill=fill, outline=fill)
            x = S(40) - r if on else r
            canvas.create_oval(x - S(8), r - S(8), x + S(8), r + S(8), fill="#f6f4ee", outline="")

        def toggle(_):
            variable.set(not variable.get())
            if command:
                command()

        canvas.bind("<Button-1>", toggle)
        variable.trace_add("write", draw)
        draw()
        return canvas

    def card(self, parent, pady=(0, 12)):
        frame = self.tk.Frame(parent, bg=view.SURFACE, padx=self.S(18), pady=self.S(14))
        frame.pack(fill="x", pady=(self.S(pady[0]), self.S(pady[1])))
        return frame

    def setting(self, parent, title, detail, variable, command=None, badge=None):
        """A switch line: badge, title and explanation on the left, switch on the right."""
        line = self.tk.Frame(parent, bg=parent["bg"])
        line.pack(fill="x")
        if badge:
            letter, color = badge
            self.tk.Label(line, text=letter, bg=color, fg="#1f1e1c", font=self.font(10, "bold"), width=2).pack(
                side="left", anchor="n", padx=(0, self.S(12)), pady=(self.S(2), 0))
        self.switch(line, variable, command).pack(side="right", anchor="n", padx=(self.S(12), 0), pady=(self.S(2), 0))
        text = self.tk.Frame(line, bg=parent["bg"])
        text.pack(side="left", fill="x", expand=True)
        self.label(text, title, 11, weight="bold").pack(anchor="w")
        line.detail = self.para(text, detail)
        line.detail.pack(anchor="w", fill="x", pady=(self.S(2), 0))
        line.text = text
        return line

    def heading(self, parent, title, subtitle="", action=None):
        top = self.tk.Frame(parent, bg=view.BG)
        top.pack(fill="x")
        if action is not None:
            self.button(top, *action).pack(side="right", anchor="n")
        self.label(top, title, 16, weight="bold").pack(side="left", anchor="w")
        under = self.tk.Frame(parent, bg=view.BG)
        under.pack(fill="x", pady=(self.S(2), self.S(14)))
        if subtitle:
            self.para(under, subtitle, 10).pack(anchor="w", fill="x")

    def section(self, parent, title, note=""):
        line = self.tk.Frame(parent, bg=view.BG)
        line.pack(fill="x", pady=(self.S(6), self.S(8)))
        self.label(line, title, 11, weight="bold").pack(side="left")
        if note:
            self.label(line, note, 9, view.INK_3).pack(side="left", padx=self.S(10))
        return line

    def entry(self, parent, variable, secret=False):
        return self.tk.Entry(parent, textvariable=variable, show="●" if secret else "", bg=view.FIELD, fg=view.INK,
                             insertbackground=view.INK, relief="flat", font=self.font(10), highlightthickness=1,
                             highlightbackground=view.BORDER, highlightcolor=view.GREEN)

    def form(self, parent, rows):
        """(variable, title, hint, secret) rows: label, field, hint."""
        grid = self.tk.Frame(parent, bg=parent["bg"])
        grid.pack(fill="x", pady=(self.S(8), 0))
        grid.columnconfigure(1, weight=1)
        for index, (variable, title, hint, secret) in enumerate(rows):
            self.label(grid, title, 10, view.INK_2).grid(row=index, column=0, sticky="w", pady=self.S(5), padx=(0, self.S(16)))
            self.entry(grid, variable, secret).grid(row=index, column=1, sticky="ew", pady=self.S(5), ipady=self.S(4))
            self.label(grid, hint, 9, view.INK_3).grid(row=index, column=2, sticky="w", padx=(self.S(12), 0))
        return grid

    def picture(self, parent, key, image, **kw):
        photo = self.ImageTk.PhotoImage(image)
        self.images[key] = photo
        widget = self.images.get(key + ":widget")
        if widget is not None and widget.winfo_exists():
            widget.configure(image=photo)
            return widget
        widget = self.tk.Label(parent, image=photo, bg=parent["bg"], bd=0, **kw)
        self.images[key + ":widget"] = widget
        return widget

    def scrollable(self, page):
        """A page body that scrolls with the mouse wheel when it outgrows the window."""
        tk = self.tk
        canvas = tk.Canvas(page, bg=view.BG, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        inner = tk.Frame(canvas, bg=view.BG)
        window = canvas.create_window(0, 0, window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _: canvas.configure(scrollregion=(0, 0, inner.winfo_reqwidth(), inner.winfo_reqheight())))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))

        def wheel(event):
            if self.pages.get(self.page) is page and inner.winfo_reqheight() > canvas.winfo_height():
                canvas.yview_scroll(-1 if (event.delta > 0 or getattr(event, "num", 0) == 4) else 1, "units")

        canvas.bind_all("<MouseWheel>", wheel, add="+")
        canvas.bind_all("<Button-4>", wheel, add="+")
        canvas.bind_all("<Button-5>", wheel, add="+")
        inner.canvas = canvas
        return inner

    # ----- layout ----------------------------------------------------------------------
    def _layout(self):
        tk, S = self.tk, self.S
        side = tk.Frame(self.root, bg=view.SIDEBAR, width=S(212))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=view.SIDEBAR)
        brand.pack(fill="x", padx=S(16), pady=(S(22), S(24)))
        dots = tk.Canvas(brand, width=S(26), height=S(26), bg=view.SIDEBAR, highlightthickness=0)
        for index, provider in enumerate(("claude", "codex", "gemini")):
            color = view.hex_color(view.accent(letter_of(provider)))
            x = S(1 + index * 7)
            dots.create_oval(x, S(8), x + S(11), S(19), fill=color, outline=view.SIDEBAR, width=S(2))
        dots.pack(side="left", anchor="n")
        names = tk.Frame(brand, bg=view.SIDEBAR)
        names.pack(side="left", padx=(S(6), 0))
        self.label(names, "CodexQuotaTray", 11, weight="bold").pack(anchor="w")
        self.label(names, "AI 额度与 TPS", 9, view.INK_3).pack(anchor="w")
        self.nav = {}
        for key, title in PAGES:
            item = tk.Frame(side, bg=view.SIDEBAR, cursor="hand2")
            item.pack(fill="x", padx=S(10), pady=S(2))
            bar = tk.Frame(item, bg=view.SIDEBAR, width=S(3))
            bar.pack(side="left", fill="y")
            text = self.label(item, title, 11, view.INK_2, padx=S(12), pady=S(9), cursor="hand2")
            text.pack(side="left", fill="x", expand=True)
            for widget in (item, text):
                widget.bind("<Button-1>", lambda _, key=key: self.show_page(key))
            self.nav[key] = (item, bar, text)
        self.label(side, "托盘右键或面板齿轮可打开此页", 8, view.INK_3, wraplength=S(180)).pack(side="bottom", anchor="w", padx=S(20), pady=S(18))

        main = tk.Frame(self.root, bg=view.BG)
        main.pack(side="left", fill="both", expand=True)
        bar = tk.Frame(main, bg=view.SIDEBAR)
        bar.pack(side="bottom", fill="x")
        footer = tk.Frame(bar, bg=view.SIDEBAR)
        footer.pack(fill="x", padx=S(28), pady=S(12))
        self.status = tk.StringVar()
        self.status_label = self.label(footer, "", 10, view.INK_2, textvariable=self.status)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.save_button = self.button(footer, "保存", self.save, "primary")
        self.save_button.pack(side="right")
        self.button(footer, "关闭", self.close).pack(side="right", padx=S(8))
        self.body = tk.Frame(main, bg=view.BG)
        self.body.pack(fill="both", expand=True, padx=S(28), pady=(S(22), S(6)))
        self.pages = {key: tk.Frame(self.body, bg=view.BG) for key, _ in PAGES}
        self.bodies = {key: self.scrollable(frame) for key, frame in self.pages.items()}
        self._overview(self.bodies["overview"])
        self._sources(self.bodies["sources"])
        self._iq(self.bodies["iq"])
        self._tasks(self.bodies["tasks"])
        self._general(self.bodies["general"])
        self.show_page("overview")

    def show_page(self, key):
        key = PAGE_ALIASES.get(key, key)
        if key not in self.pages:
            return
        self.page = key
        for name, frame in self.pages.items():
            if name != key and frame.winfo_manager():
                frame.place_forget()
        self.pages[key].place(relx=0, rely=0, relwidth=1, relheight=1)
        for name, (item, bar, text) in self.nav.items():
            on = name == key
            item.configure(bg=view.SURFACE if on else view.SIDEBAR)
            text.configure(bg=view.SURFACE if on else view.SIDEBAR, fg=view.INK if on else view.INK_2)
            bar.configure(bg=view.GREEN if on else view.SIDEBAR)
        self.pages[key].tkraise()
        self.bodies[key].canvas.yview_moveto(0)
        self.refresh_live()

    # ----- 概览 ---------------------------------------------------------------------------
    def _overview(self, page):
        tk, S = self.tk, self.S
        self.heading(page, "概览", "与托盘和悬停面板同步：每行标题是距下次重置的时间，颜色与托盘图标一致；"
                                   "卡片底部是下次读取的时间与原因。点击卡片查看额度详情。",
                     ("立即刷新", self.app.request_refresh))
        self.tray_notice = tk.Frame(page, bg="#3a3122", padx=S(16), pady=S(10))
        self.button(self.tray_notice, "打开任务栏设置", self.open_taskbar_settings).pack(side="right", padx=(S(12), 0))
        self.tray_notice_text = self.label(self.tray_notice, "", 10, view.WARN, bg="#3a3122")
        self.tray_notice.bind("<Configure>", lambda event: self.tray_notice_text.configure(
            wraplength=max(S(200), event.width - S(190))), add="+")
        self.tray_notice_text.pack(side="left", fill="x", expand=True)
        self.card_grid = tk.Frame(page, bg=view.BG)
        self.card_grid.pack(fill="x")
        self.chart_holder = tk.Frame(page, bg=view.BG)
        self.chart_holder.pack(fill="x", pady=(S(2), 0))

    def _fill_overview(self):
        S = self.S
        hidden = int(getattr(self.app, "hidden_slots", 0) or 0)
        if hidden:
            text = f"有 {hidden} 个托盘图标收在 ^ 隐藏区，任务栏上会缺少排在最后的来源。请在任务栏设置中打开全部 CodexQuotaTray 图标。"
            self.set_text(self.tray_notice_text, text)
            if not self.tray_notice.winfo_manager():
                self.tray_notice.pack(fill="x", pady=(0, S(12)), before=self.card_grid)
        elif self.tray_notice.winfo_manager():
            self.tray_notice.pack_forget()
        width = self.body.winfo_width() or S(780)
        columns = max(1, min(3, (width + S(14)) // (S(view.CARD_W) + S(14))))
        cards = build_cards(self.app)
        keys = [card.provider for card, _ in cards]
        if self.images.get("overview:keys") != (keys, columns):
            for child in self.card_grid.winfo_children():
                child.destroy()
            for key in list(self.images):
                if key.startswith("card:"):
                    self.images.pop(key)
            self.images["overview:keys"] = (keys, columns)
            for column in range(3):
                self.card_grid.grid_columnconfigure(column, weight=1 if column < columns else 0,
                                                    uniform="sourcecards" if column < columns else "")
        slots = {}
        for index, (card, _icon) in enumerate(cards):
            row = index // columns
            slots[row] = max(slots.get(row, 1), len(card.rows) or 1)
        for index, (card, icon) in enumerate(cards):
            key = "card:" + card.provider
            card_slots = slots[index // columns]
            self._card_models[card.provider] = (card, icon, card_slots)
            image = view.source_card(card, icon, self.scale, hover=self.hover_card == card.provider, slots=card_slots)
            widget = self.picture(self.card_grid, key, image, cursor="hand2")
            if not widget.grid_info():
                widget.grid(row=index // columns, column=index % columns, padx=(0, S(14)), pady=(0, S(14)), sticky="nw")
                widget.bind("<Button-1>", lambda _, p=card.provider: self.app.open_details(p))
                widget.bind("<Enter>", lambda _, p=card.provider: self._hover(p))
                widget.bind("<Leave>", lambda _: self._hover(None))
        self._painted_hover = self.hover_card
        events = []
        try:
            events = list(self.app.tps_monitor.events)
        except (AttributeError, RuntimeError):
            pass
        letters = {provider: letter_of(provider) for provider in in_tray_order(self.app.snapshots)}
        chart_width = max(view.CARD_W, round(width / self.scale) - 2)
        chart = view.activity_chart(events, time.time(), letters, self.scale, width=max(view.CARD_W, chart_width), height=136)
        widget = self.picture(self.chart_holder, "chart", chart)
        if not widget.winfo_manager():
            widget.pack(anchor="w")

    def _hover(self, provider):
        self.hover_card = provider
        if self._hover_after is not None:
            self.root.after_cancel(self._hover_after)
        self._hover_after = self.root.after(60, self._finish_hover)

    def _finish_hover(self):
        self._hover_after = None
        if self.page == "overview" and self._resize_after is None:
            for provider in {self._painted_hover, self.hover_card} - {None}:
                cached = self._card_models.get(provider)
                if cached is None:
                    continue
                card, icon, slots = cached
                image = view.source_card(card, icon, self.scale, hover=self.hover_card == provider, slots=slots)
                self.picture(self.card_grid, "card:" + provider, image, cursor="hand2")
            self._painted_hover = self.hover_card

    def open_taskbar_settings(self):
        if hasattr(os, "startfile"):
            os.startfile("ms-settings:taskbar")  # type: ignore[attr-defined]  # the user's own settings page

    # ----- 来源 ---------------------------------------------------------------------------
    def _sources(self, page):
        import providers
        tk, S = self.tk, self.S
        self.heading(page, "来源", "开关在点“保存”后生效，托盘会自动重启；MiMo 的登录和手动套餐点击后立即生效。")
        if os.environ.get("CODEX_QUOTA_PROVIDERS"):
            self.para(page, f"已设置 CODEX_QUOTA_PROVIDERS={os.environ['CODEX_QUOTA_PROVIDERS']}；本页开关优先于它。", 9, view.WARN).pack(
                anchor="w", fill="x", pady=(0, S(10)))
        states = {state["key"]: state for state in providers.provider_states()}
        self.source_rows = {}
        box = self.card(page)
        for index, key in enumerate(in_tray_order(states)):
            state = states[key]
            if index:
                tk.Frame(box, bg=view.BORDER, height=1).pack(fill="x", pady=S(12))
            self.vars["source:" + key] = tk.BooleanVar(value=state["enabled"])
            self.vars["source:" + key].trace_add("write", lambda *_: self.update_footer())
            row = self.setting(box, PROVIDER_NAMES.get(key, key), state["description"], self.vars["source:" + key],
                               command=self._fill_sources, badge=(state["letter"], view.hex_color(view.accent(state["letter"]))))
            row.state = self.para(row.text, "", 9, view.INK_2)
            row.state.pack(anchor="w", fill="x", pady=(self.S(4), 0))
            self.source_rows[key] = row
            if key == "mimo":
                self._mimo(box)
        self.section(page, "本机记录", "各读取器使用的本机数据")
        self.health_box = tk.Frame(page, bg=view.BG)
        self.health_box.pack(fill="x")

    def _fill_sources(self):
        import providers
        for state in providers.provider_states():
            row = self.source_rows.get(state["key"])
            if row is None:
                continue
            pending = bool(self.vars["source:" + state["key"]].get()) != state["enabled"]
            text = ("当前已开启" if state["enabled"] else "当前已关闭") + f" · {state['reason']}" + ("　→ 保存后重启生效" if pending else "")
            self.set_text(row.state, text, fg=view.WARN if pending else view.INK_2)
        self._fill_health()

    def _mimo(self, box):
        tk, S = self.tk, self.S
        block = tk.Frame(box, bg=view.FIELD, padx=S(14), pady=S(12))
        block.pack(fill="x", pady=(S(10), 0), padx=(S(36), 0))
        self.mimo_head = self.label(block, "", 10, weight="bold")
        self.mimo_head.pack(anchor="w")
        self.mimo_info = self.para(block, "", 9, view.INK_3)      # shown only when it has something to say
        self.mimo_warn = self.para(block, "", 9, view.WARN)
        line = tk.Frame(block, bg=view.FIELD)
        line.pack(fill="x", pady=(S(10), 0))
        self.button(line, "登录小米 MiMo", self.mimo_login, "primary").pack(side="left")
        self.button(line, "退出登录", self.mimo_logout).pack(side="left", padx=S(8))
        self.mimo_toggle = self.link(line, "手动填写套餐 ▸", self.toggle_mimo_manual)
        self.mimo_toggle.pack(side="left", padx=S(8))
        self.para(block, "登录会打开一个独立的 Edge/Chrome 窗口进入 platform.xiaomimimo.com；进入套餐页后自动读取并关闭。"
                         "会话用 Windows 加密保存在本机（mimo-session.bin），不保存账号密码。", 9, view.INK_3).pack(
            anchor="w", fill="x", pady=(S(8), 0))
        self.mimo_manual = tk.Frame(block, bg=view.FIELD)
        self.mimo_fields = {name: tk.StringVar() for name in ("total", "end", "used", "name")}
        self.label(self.mimo_manual, "不登录、免费额度或登录失败时使用：只统计本机用量", 9, view.INK_3).pack(anchor="w", pady=(S(10), 0))
        self.form(self.mimo_manual, ((self.mimo_fields["total"], "套餐总量", "例如 49.2B、492亿", False),
                                     (self.mimo_fields["end"], "到期日期", "YYYY-MM-DD", False),
                                     (self.mimo_fields["used"], "已用量（可选）", "控制台当前已用", False),
                                     (self.mimo_fields["name"], "套餐名（可选）", "例如 Max 年付", False)))
        buttons = tk.Frame(self.mimo_manual, bg=view.FIELD)
        buttons.pack(fill="x", pady=(S(8), 0))
        self.button(buttons, "保存手动套餐", self.save_mimo_manual).pack(side="left")
        self.button(buttons, "删除手动套餐", self.remove_mimo_manual).pack(side="left", padx=S(8))
        self.label(buttons, "点击后立即生效", 9, view.INK_3).pack(side="left", padx=S(4))

    def toggle_mimo_manual(self):
        shown = bool(self.mimo_manual.winfo_manager())
        if shown:
            self.mimo_manual.pack_forget()
        else:
            self.mimo_manual.pack(fill="x")
        self.mimo_toggle.configure(text="手动填写套餐 ▾" if not shown else "手动填写套餐 ▸")

    def _fill_mimo(self):
        info = self.app.mimo_status() if hasattr(self.app, "mimo_status") else {}
        manual = info.get("manual")
        if info.get("logged_in"):
            head, color = "✓ 已登录控制台", view.GREEN
        elif manual is not None:
            head, color = "✓ 使用手动套餐", view.GREEN
        else:
            head, color = "尚未配置：登录控制台，或手动填写套餐", view.WARN
        total, used = info.get("total"), info.get("used")
        parts = []
        if total:
            parts.append(f"套餐 {info.get('plan') or ''} 共 {total / 1e9:.2f}B" + (f"，已用 {used / 1e9:.2f}B" if used is not None else ""))
        if not info.get("browser"):
            parts.append("未找到 Edge/Chrome：登录需要其中之一，或设置 CODEX_QUOTA_MIMO_BROWSER 指向浏览器")
        warn = info.get("login_result") or info.get("error") or ""
        for widget, text, ink in ((self.mimo_head, head, color), (self.mimo_info, " · ".join(parts), view.INK_3),
                                  (self.mimo_warn, warn, view.WARN)):
            self.set_text(widget, text, fg=ink)
        for widget, after in ((self.mimo_info, self.mimo_head), (self.mimo_warn, self.mimo_info)):
            if widget.cget("text") and not widget.winfo_manager():
                widget.pack(anchor="w", fill="x", after=after)
            elif not widget.cget("text") and widget.winfo_manager():
                widget.pack_forget()
        if manual is not None and not self.mimo_manual.winfo_manager() and not info.get("logged_in"):
            self.toggle_mimo_manual()

    def mimo_login(self):
        self.app.mimo_login()
        self.root.after(300, self._fill_mimo)

    def mimo_logout(self):
        self.app.mimo_logout()
        self._fill_mimo()

    def save_mimo_manual(self):
        values = {name: var.get() for name, var in self.mimo_fields.items()}
        try:
            self.app.save_mimo_manual(values["total"], values["end"], values["used"], values["name"])
            self.say("✓ 手动套餐已保存")
        except (OSError, ValueError) as error:
            self.messagebox.showerror("未能保存", str(error), parent=self.root)
        self._fill_mimo()

    def remove_mimo_manual(self):
        import mimo_usage
        mimo_usage.remove_manual_plan()
        self.say("已删除手动套餐")
        self._fill_mimo()

    def _fill_health(self):
        tk, S = self.tk, self.S
        health = (getattr(self.app, "performance", None) or {}).get("source_health", {}) or {}
        signature = tuple(sorted((name, str((value or {}).get("status")), str((value or {}).get("sessions")))
                                 for name, value in health.items()))
        if self.images.get("health:signature") == signature:
            return
        self.images["health:signature"] = signature
        for child in self.health_box.winfo_children():
            child.destroy()
        tones = {"ok": (view.GREEN, "#23342c"), "error": ("#ef8a7f", "#3a2524"), "idle": (view.INK_3, view.SURFACE_HI)}
        grid = tk.Frame(self.health_box, bg=view.BG)
        grid.pack(fill="x")
        for index, (name, value) in enumerate(sorted(health.items())):
            status = str((value or {}).get("status") or "未知")
            ink, fill = tones[view.health_tone(status)]
            chip = tk.Frame(grid, bg=view.SURFACE, padx=S(12), pady=S(9))
            chip.grid(row=index // 3, column=index % 3, sticky="nsew", padx=(0, S(8)), pady=(0, S(8)))
            grid.columnconfigure(index % 3, weight=1, uniform="health")
            self.label(chip, name, 10, weight="bold").pack(anchor="w")
            line = tk.Frame(chip, bg=view.SURFACE)
            line.pack(anchor="w", pady=(S(3), 0))
            tk.Label(line, text=" " + status + " ", bg=fill, fg=ink, font=self.font(8)).pack(side="left")
            extra = value.get("sessions") if isinstance(value, dict) else None
            if extra is not None:
                self.label(line, f"  {extra} 个会话", 8, view.INK_3).pack(side="left")
        if not health:
            self.label(self.health_box, "正在收集…", 10, view.INK_3).pack(anchor="w")

    # ----- 模型智商 -------------------------------------------------------------------------
    def _iq(self, page):
        import grok_iq
        import model_iq
        tk, S = self.tk, self.S
        self.heading(page, "模型智商", "悬停面板顶部横条右侧显示所选模型的编程分；Grok 面板中间一栏显示 Grok 相对最强模型的总分。")
        self.section(page, "面板横条", "AI Stupid Level · 近 24 小时编程分（0–100）")
        box = self.card(page)
        self.iq_key = tk.StringVar()
        self.iq_key.trace_add("write", lambda *_: self.update_footer())
        self.iq_built = None
        self.iq_checks = {}
        self.form(box, ((self.iq_key, "API Key", "asl_live_…，仅保存在本机", True),))
        line = tk.Frame(box, bg=view.SURFACE)
        line.pack(fill="x", pady=(S(10), 0))
        self.button(line, "获取免费 Key", lambda: os.startfile(model_iq.SIGNUP)).pack(side="left")  # type: ignore[attr-defined]
        self.button(line, "立即读取", self.iq_refresh, "primary").pack(side="left", padx=S(8))
        self.iq_message = tk.StringVar()
        self.label(line, "", 9, view.INK_2, textvariable=self.iq_message).pack(side="left", padx=S(8))
        self.iq_info = self.para(box, "", 9, view.INK_3)
        self.iq_info.pack(anchor="w", fill="x", pady=(S(8), 0))
        self.para(box, "每 3 小时自动读取一次；免费 Key 每天 10 次，自动最多用 7 次，其余留给“立即读取”。", 9, view.INK_3).pack(
            anchor="w", fill="x", pady=(S(2), 0))
        self.section(page, "Grok 智商栏", "LiveBench · 以榜上最高分为 100%")
        grok = self.card(page)
        top = tk.Frame(grok, bg=view.SURFACE)
        top.pack(fill="x")
        self.grok_figure = self.label(top, "—", 22, view.INK, weight="bold")
        self.grok_figure.pack(side="left")
        words = tk.Frame(top, bg=view.SURFACE)
        words.pack(side="left", padx=S(14))
        self.grok_caption = self.label(words, "", 11, weight="bold")
        self.grok_caption.pack(anchor="w")
        self.grok_info = self.label(words, "", 9, view.INK_3)
        self.grok_info.pack(anchor="w")
        actions = tk.Frame(top, bg=view.SURFACE)
        actions.pack(side="right")
        self.button(actions, "立即读取", self.grok_refresh).pack(side="right")
        self.grok_message = tk.StringVar()
        self.label(actions, "", 9, view.INK_2, textvariable=self.grok_message).pack(side="right", padx=S(8))
        self.para(grok, "Grok 不在 AI Stupid Level 的榜单上，所以改用 LiveBench 最新公开榜单的加权总分；"
                        "每 3 小时检查一次，只有出现新榜单时才重新下载。", 9, view.INK_3).pack(anchor="w", fill="x", pady=(S(10), 0))
        self.link(grok, "数据来源：livebench.ai", lambda: os.startfile(grok_iq.SITE)).pack(anchor="w", pady=(S(6), 0))  # type: ignore[attr-defined]

        pick = self.section(page, "横条显示的模型")
        self.button(pick, "恢复默认", self.iq_default).pack(side="right")
        self.iq_pick = self.label(pick, "", 9, view.INK_3)
        self.iq_pick.pack(side="left", padx=S(10))
        self.iq_table = tk.Frame(page, bg=view.SURFACE, padx=S(14), pady=S(10))
        self.iq_table.pack(fill="x")
        source = self.link(page, "数据来源：aistupidlevel.info（AI Stupid Level）", lambda: os.startfile(model_iq.SITE))  # type: ignore[attr-defined]
        source.pack(anchor="w", pady=(S(8), 0))

    def iq_refresh(self):
        if not hasattr(self.app, "refresh_model_iq"):
            return
        try:
            self.app.save_model_iq(self.iq_key.get(), list(self.iq_selected) or None)
        except (OSError, ValueError) as error:
            self.messagebox.showerror("未能保存", str(error), parent=self.root)
            return
        self.iq_message.set("正在读取…")

        def worker():
            try:
                text = self.app.refresh_model_iq()
            except Exception as error:
                text = "读取失败：" + type(error).__name__
            self.commands.put(("iq", text))
        threading.Thread(target=worker, name="model-iq-manual", daemon=True).start()

    def grok_refresh(self):
        if not hasattr(self.app, "refresh_grok_iq"):
            return
        self.grok_message.set("正在读取…")

        def worker():
            try:
                text = self.app.refresh_grok_iq()
            except Exception as error:
                text = "读取失败：" + type(error).__name__
            self.commands.put(("grok", text))
        threading.Thread(target=worker, name="grok-iq-manual", daemon=True).start()

    def iq_default(self):
        self.iq_selected = []
        for var in self.iq_checks.values():
            var.set(False)
        self._fill_iq(rebuild=False)
        self.update_footer()

    def iq_toggle(self, name):
        import model_iq
        var = self.iq_checks[name]
        if var.get():
            if len(self.iq_selected) >= model_iq.HOVER_MAX:
                var.set(False)
                self.iq_message.set(f"横条最多显示 {model_iq.HOVER_MAX} 个模型")
                return
            self.iq_selected.append(name)
        elif name in self.iq_selected:
            self.iq_selected.remove(name)
        self._fill_iq(rebuild=False)
        self.update_footer()

    def _fill_iq(self, rebuild=True):
        import grok_iq
        import model_iq
        tk, S = self.tk, self.S
        state = getattr(self.app, "grok_iq", None)
        _label, figure, caption, unit = grok_iq.column(state)
        info = []
        if state is not None and state.release:
            info.append(f"榜单 {state.release}")
        if state is not None and state.fetched_at:
            info.append("上次读取 " + time.strftime("%m-%d %H:%M", time.localtime(state.fetched_at)))
        if state is not None and state.error:
            info.append(state.error)
        for widget, text in ((self.grok_figure, figure + unit), (self.grok_caption, caption),
                             (self.grok_info, " · ".join(info) or "尚未读取")):
            if widget.cget("text") != text:
                widget.configure(text=text)
        if not hasattr(self.app, "model_iq_status"):
            return
        status = self.app.model_iq_status()
        parts = [f"上次读取：{status['fetched']}", f"今日已用 {status['calls']} / {model_iq.DAILY_LIMIT} 次（00:00 UTC 重置）"]
        if status["env_key"]:
            parts.append(f"已设置 {model_iq.ENV_KEY} 环境变量，优先于本页的 Key")
        if status["error"]:
            parts.append("⚠ " + status["error"])
        self.set_text(self.iq_info, " · ".join(parts))
        shown = self.iq_selected or status["default"]
        pick = ("、".join(model_iq.short_name(n) for n in shown) or "—") + \
            ("（默认：编程分最高的 3 个，正在运行的模型始终在最前）" if not self.iq_selected else "") + f"；最多 {model_iq.HOVER_MAX} 个"
        if self.iq_pick.cget("text") != pick:
            self.iq_pick.configure(text=pick)
        if rebuild and self.iq_built != status["fetched_at"]:
            self.iq_built = status["fetched_at"]
            for child in self.iq_table.winfo_children():
                child.destroy()
            self.iq_checks = {}
            models = status["models"]
            if not models:
                self.label(self.iq_table, "填写 API Key 后点“立即读取”，这里会列出所有模型的分数。", 10, view.INK_3).grid(row=0, column=0, sticky="w")
                return
            for column, title in enumerate(("显示", "#", "模型", "厂商", "编程分")):
                self.label(self.iq_table, title, 9, view.INK_3).grid(row=0, column=column, sticky="w", padx=(0, S(14)), pady=(0, S(4)))
            self.iq_table.columnconfigure(2, weight=1)
            for index, score in enumerate(models, 1):
                var = self.iq_checks[score.name] = tk.BooleanVar(value=score.name in self.iq_selected)
                tk.Checkbutton(self.iq_table, variable=var, command=lambda name=score.name: self.iq_toggle(name), bg=view.SURFACE,
                               activebackground=view.SURFACE, selectcolor=view.FIELD, fg=view.INK, bd=0,
                               highlightthickness=0).grid(row=index, column=0, sticky="w")
                cells = (str(index), f"{score.short}  ({score.name})" if score.short != score.name else score.name,
                         model_iq.provider_label(score.provider), model_iq.score_text(score))
                for column, value in enumerate(cells, 1):
                    color = view.INK if column in (2, 4) else view.INK_2
                    self.label(self.iq_table, value, 10 if column in (2, 4) else 9, color,
                               weight="bold" if column == 4 else "normal").grid(row=index, column=column, sticky="w", padx=(0, S(14)))

    # ----- 任务 ---------------------------------------------------------------------------
    def _tasks(self, page):
        tk, S = self.tk, self.S
        self.heading(page, "任务", "任务气泡来自本机 agent 记录；使用 herdr 时，同一窗格的会话合并为一个气泡。")
        box = self.card(page)
        self.vars["herdr"] = tk.BooleanVar(value=True)
        self.vars["herdr"].trace_add("write", lambda *_: self.update_footer())
        self.herdr_row = self.setting(box, "herdr 适配", "", self.vars["herdr"])
        self.agent_holder = tk.Frame(page, bg=view.BG)
        self.agent_holder.pack(fill="x", pady=(0, S(14)))

        self.section(page, "任务命名", "可选：用 OpenAI 兼容接口（含 MiMo Token Plan）把请求归纳为气泡标题")
        naming = self.card(page)
        self.vars["summary"] = tk.BooleanVar()
        self.vars["summary"].trace_add("write", lambda *_: self.update_footer())
        self.setting(naming, "启用模型命名", "启用后发送每个成员最近一次请求的前 400 字、项目文件夹名和当前动作。密钥只保存在本机。",
                     self.vars["summary"])
        self.fields = {name: tk.StringVar() for name in ("url", "key", "model")}
        for variable in self.fields.values():
            variable.trace_add("write", lambda *_: self.update_footer())
        self.form(naming, ((self.fields["url"], "API 地址", "https://…/v1", False), (self.fields["key"], "API Key", "仅保存在本机", True),
                           (self.fields["model"], "模型", "例如 mimo-v2-flash", False)))
        test = tk.Frame(naming, bg=view.SURFACE)
        test.pack(fill="x", pady=(S(10), 0))
        self.test_button = self.button(test, "测试连接", self.test_connection)
        self.test_button.pack(side="left")
        self.result = tk.StringVar()
        self.label(test, "", 10, view.INK_2, textvariable=self.result).pack(side="left", padx=S(12))
        if any(os.environ.get("CODEX_QUOTA_SUMMARY_" + name) for name in ("URL", "KEY", "MODEL")):
            self.para(naming, "已设置 CODEX_QUOTA_SUMMARY_* 环境变量，环境变量优先于本页配置。", 9, view.WARN).pack(
                anchor="w", fill="x", pady=(S(10), 0))

    def _fill_tasks(self):
        herdr = getattr(getattr(self.app, "tps_monitor", None), "herdr", None)
        if herdr is not None:
            env = os.environ.get("CODEX_QUOTA_HERDR")
            text = (f"状态：{herdr.status} · {len(herdr.rows)} 个窗格。同一窗格的会话合并为一个气泡，blocked 显示为“等你确认”；"
                    "点击 Grok 图标会在 herdr 中新建 grok 标签页。")
            if env:
                text += f"（环境变量 CODEX_QUOTA_HERDR={env} 优先）"
            self.set_text(self.herdr_row.detail, text)
        letters = {provider: letter_of(provider) for provider in PROVIDER_NAMES}
        agents = [agent for agent in (getattr(self.app, "agents", None) or []) if not agent.get("schedule")]
        width = round((self.body.winfo_width() or self.S(780)) / self.scale) - 20
        widget = self.picture(self.agent_holder, "agents", view.agent_rows(agents, letters, self.scale, width=max(400, width)))
        if not widget.winfo_manager():
            widget.pack(anchor="w")

    # ----- 常规 ---------------------------------------------------------------------------
    def _general(self, page):
        import providers
        import refresh_scheduler
        tk, S = self.tk, self.S
        self.heading(page, "常规", "启动、额度读取节奏、诊断与退出。")
        box = self.card(page)
        self.vars["startup"] = tk.BooleanVar()
        self.vars["startup"].trace_add("write", lambda *_: self.update_footer())
        self.setting(box, "开机自启", "登录 Windows 后自动在托盘显示。", self.vars["startup"])

        self.section(page, "额度读取节奏", "光标停在图标或面板上最快，正在使用其次，静默最慢")
        cadence = self.card(page)
        grid = tk.Frame(cadence, bg=view.SURFACE)
        grid.pack(fill="x")
        for column, title in enumerate(("来源", "静默", "使用中", "光标停留", "下次读取")):
            self.label(grid, title, 9, view.INK_3).grid(row=0, column=column, sticky="w", padx=(0, S(20)), pady=(0, S(6)))
        grid.columnconfigure(4, weight=1)
        self.cadence_next = {}
        for row, key in enumerate(in_tray_order([spec.key for spec in providers.SPECS]), 1):
            letter = letter_of(key)
            name = tk.Frame(grid, bg=view.SURFACE)
            name.grid(row=row, column=0, sticky="w", padx=(0, S(20)), pady=S(3))
            tk.Label(name, text=letter, bg=view.hex_color(view.accent(letter)), fg="#1f1e1c", font=self.font(9, "bold"), width=2).pack(side="left")
            self.label(name, PROVIDER_NAMES.get(key, key), 10).pack(side="left", padx=(S(8), 0))
            for column, table in enumerate((refresh_scheduler.IDLE, refresh_scheduler.ACTIVE, refresh_scheduler.HOVER), 1):
                self.label(grid, interval_text(table[key]), 10, view.INK_2).grid(row=row, column=column, sticky="w", padx=(0, S(20)))
            self.cadence_next[key] = self.label(grid, "", 9, view.INK_3)
            self.cadence_next[key].grid(row=row, column=4, sticky="w")
        self.para(cadence, "Grok 每次读取要开一个 CLI 会话，所以间隔最长；任何来源两次读取之间至少隔开上一次读取用时的 2 倍。"
                           "读取失败按 30 秒、1、2、5 分钟退避；生成刚结束或重置时间一到会提前读取。", 9, view.INK_3).pack(
            anchor="w", fill="x", pady=(S(10), 0))

        self.section(page, "工具", "")
        tools = self.card(page)
        line = tk.Frame(tools, bg=view.SURFACE)
        line.pack(fill="x")
        for text, command in (("诊断信息", self.app.open_diagnostics), ("全局重置预测", self.app.open_reset_page),
                              ("全部 AI 速率", self.app.open_all_performance), ("打开数据文件夹", self.open_data_folder)):
            self.button(line, text, command).pack(side="left", padx=(0, S(8)))
        danger = self.card(page)
        self.button(danger, "退出全部", self.app.exit_app, "danger").pack(side="right")
        self.label(danger, "退出", 11, weight="bold").pack(anchor="w")
        self.label(danger, "关闭全部托盘图标与后台读取。", 9, view.INK_3).pack(anchor="w")

    def _fill_general(self):
        scheduler = getattr(self.app, "scheduler", None)
        now = time.time()
        for key, widget in self.cadence_next.items():
            text = "未开启"
            if scheduler is not None and key in getattr(scheduler, "providers", ()):
                text = view.refresh_text(*scheduler.plan(key, now)) or "等待"
            if widget.cget("text") != text:
                widget.configure(text=text)

    # ----- live content --------------------------------------------------------------------
    def refresh_live(self):
        if self.root.state() in ("withdrawn", "iconic") or self._resize_after is not None:
            return
        try:
            {"overview": self._fill_overview, "sources": self._fill_sources_all, "iq": self._fill_iq,
             "tasks": self._fill_tasks, "general": self._fill_general}[self.page]()
        except Exception:
            logging.getLogger(__name__).exception("settings refresh failed")
        self.update_footer()

    def _fill_sources_all(self):
        self._fill_sources()
        self._fill_mimo()

    # ----- staged changes ------------------------------------------------------------------------
    def staged(self) -> dict:
        values = {name: bool(var.get()) for name, var in self.vars.items()}
        values.update({"field:" + name: var.get().strip() for name, var in self.fields.items()})
        values["iq:key"] = self.iq_key.get().strip()
        values["iq:shown"] = tuple(self.iq_selected)
        return values

    def pending(self) -> list[str]:
        labels = {"herdr": "herdr 适配", "startup": "开机自启", "summary": "任务命名", "iq:key": "智商 API Key",
                  "iq:shown": "横条显示的模型"}
        changed, seen = [], set()
        for name, value in self.staged().items():
            if self.origins.get(name, value) == value:
                continue
            if name.startswith("source:"):
                text = PROVIDER_NAMES.get(name[7:], name[7:]) + " 开关"
            elif name.startswith("field:"):
                text = "任务命名"
            else:
                text = labels.get(name, name)
            if text not in seen:
                seen.add(text)
                changed.append(text)
        return changed

    def say(self, text: str):
        self.notice = (text, time.time())
        self.update_footer()

    def update_footer(self):
        if not hasattr(self, "save_button") or not self.origins:
            return
        changed = self.pending()
        self.save_button.set_enabled(bool(changed))
        notice, at = self.notice
        if notice and time.time() - at < NOTICE_SECONDS:
            text, color = notice, view.GREEN if notice.startswith("✓") else view.INK_2
        elif changed:
            restart = any(item.endswith(" 开关") for item in changed)
            text = "未保存：" + "、".join(changed) + ("（保存后托盘会重启）" if restart else "")
            color = view.WARN
        else:
            text, color = "", view.INK_2
        if self.status.get() != text:
            self.status.set(text)
        if self.status_label.cget("fg") != color:
            self.status_label.configure(fg=color)

    # ----- actions -------------------------------------------------------------------------------
    def open_data_folder(self):
        folder = agent_tasks.SUMMARY_CONFIG.parent
        folder.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(folder)  # noqa: S606 - user-requested local folder

    def test_connection(self):
        config = {name: value.get().strip() for name, value in self.fields.items()}
        address = urlsplit(config["url"])
        if address.scheme not in ("http", "https") or not address.hostname or not config["model"]:
            self.result.set("请先填写 API 地址与模型")
            return
        config["url"] = config["url"].rstrip("/")
        if not config["url"].endswith("/chat/completions"):
            config["url"] += "/chat/completions"
        self.result.set("正在测试连接…")

        def worker():
            try:
                cluster = agent_tasks.Cluster("settings-test", ({"key": "settings-test", "request": "测试任务命名连接", "title": "连接测试", "doing": "检查接口"},))
                reply = agent_tasks.Summarizer(config).request(cluster)
                text = "连接成功，已收到任务名称" if reply.get("title") else "API 已响应，但没有返回有效任务名称"
            except Exception as error:
                # Do not expose endpoint responses or credentials in dialogs/logs.
                text = "连接失败：" + type(error).__name__ + (f"（HTTP {error.code}）" if hasattr(error, "code") else "")
            self.commands.put(("result", text))
        threading.Thread(target=worker, name="summary-test", daemon=True).start()

    def load(self):
        import providers
        config = load_summary()
        for name, value in self.fields.items():
            value.set(config[name])
        self.vars["summary"].set(config["enabled"])
        self.vars["startup"].set(self.app.startup_enabled())
        for state in providers.provider_states():
            if "source:" + state["key"] in self.vars:
                self.vars["source:" + state["key"]].set(state["enabled"])
        manual = {}
        try:
            import mimo_usage
            manual = json.loads(mimo_usage.CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, ImportError):
            pass
        for name, key in (("total", "total_credits"), ("end", "period_end"), ("used", "used_credits"), ("name", "plan_name")):
            value = manual.get(key, "") if isinstance(manual, dict) else ""
            self.mimo_fields[name].set(f"{value / 1e9:g}B" if name in ("total", "used") and isinstance(value, (int, float)) else str(value or ""))
        herdr = getattr(getattr(self.app, "tps_monitor", None), "herdr", None)
        self.vars["herdr"].set(bool(herdr.enabled) if herdr is not None else True)
        if hasattr(self.app, "model_iq_status"):
            info = self.app.model_iq_status()
            self.iq_key.set(info["key"])
            self.iq_selected = list(info["shown"] or [])
            self.iq_built = None
        self.origins = self.staged()
        self.notice = ("", 0.0)
        self.result.set("")
        self.update_footer()

    def save(self):
        if not self.pending():
            return
        try:
            self.app.apply_summary_settings({name: var.get() for name, var in self.fields.items()}, self.vars["summary"].get())
            if self.vars["startup"].get() != self.origins["startup"]:
                self.app.toggle_startup()
            if hasattr(self.app, "save_model_iq"):
                self.app.save_model_iq(self.iq_key.get(), list(self.iq_selected) or None)
            if hasattr(self.app, "save_preferences"):
                self.app.save_preferences({"herdr": bool(self.vars["herdr"].get())})
            sources = {name[7:]: bool(var.get()) for name, var in self.vars.items()
                       if name.startswith("source:") and bool(var.get()) != self.origins.get(name)}
            self.origins = self.staged()
            if sources:
                self.say("✓ 已保存，托盘正在重启以应用来源开关…")
                self.root.withdraw()
                self.app.set_providers(sources)
            else:
                self.say("✓ 设置已保存并生效")
        except (OSError, ValueError) as error:
            self.messagebox.showerror("未能保存", str(error), parent=self.root)

    def close(self):
        """Hide the window; unsaved switches and fields are offered for saving first."""
        changed = self.pending() if self.origins else []
        if changed:
            answer = self.messagebox.askyesnocancel("保存更改？", "以下更改还没有保存：\n" + "、".join(changed) + "\n\n保存后关闭？",
                                                    parent=self.root)
            if answer is None:
                return
            if answer:
                self.save()
                if self.pending():
                    return
        self.root.withdraw()

    # ----- loop ------------------------------------------------------------------------------------
    def run(self):
        self.ticks = 0

        def poll():
            if self.app.stop_event.is_set():
                self.root.destroy()
                return
            while True:
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    break
                if command[0] == "close":
                    self.root.destroy()
                    return
                if command[0] == "show":
                    if self.root.state() == "withdrawn":
                        self.load()
                    self.root.deiconify()
                    self.root.lift()
                    if len(command) > 1 and command[1]:
                        self.show_page(command[1])
                    self.root.after(60, self.refresh_live)
                elif command[0] == "result":
                    self.result.set(command[1])
                elif command[0] == "iq":
                    self.iq_message.set(command[1])
                    self.iq_built = None
                    self._fill_iq()
                elif command[0] == "grok":
                    self.grok_message.set(command[1])
                    self._fill_iq(rebuild=False)
            self.ticks += 1
            if self.ticks % 20 == 0:          # live figures every 2 s
                self.refresh_live()
            self.root.after(100, poll)

        poll()
        self.root.mainloop()
