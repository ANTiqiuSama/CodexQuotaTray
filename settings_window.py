"""User-opened settings window; secrets stay in the local data directory."""
from __future__ import annotations

import json
import os
import logging
import queue
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


PAGES = (("overview", "概览"), ("sources", "额度来源"), ("agents", "任务与 herdr"), ("naming", "任务命名 API"), ("general", "常规"))
PROVIDER_NAMES = {"codex": "Codex", "grok": "Grok", "gemini": "Gemini", "claude": "Claude", "mimo": "Xiaomi MiMo"}


def letter_of(provider: str) -> str:
    import providers
    spec = providers.BY_KEY.get(provider)
    return spec.letter if spec else "?"


def build_cards(app, now: float | None = None) -> list[tuple[view.SourceCard, object]]:
    """(card, current tray icon image) for every shown source."""
    now = now or time.time()
    result = []
    performance = (getattr(app, "performance", None) or {}).get("providers", {})
    for provider, snapshot in app.snapshots.items():
        data = performance.get(provider, {})
        model = None
        try:
            model = app.panel(provider)
        except Exception:
            model = None
        letter = letter_of(provider)
        if model is not None:
            status, tone, forecast, forecast_tone = model.status, model.status_tone, model.forecast, model.forecast_tone
        else:
            status, tone, forecast, forecast_tone = ("未开通" if snapshot is not None and snapshot.error else "空闲"), "idle", "", "unknown"
        note = (snapshot.error or "") if snapshot is not None else ""
        card = view.SourceCard(provider, letter, PROVIDER_NAMES.get(provider, provider), status, tone, view.quota_rows(snapshot, now),
                               data.get("display_tps"), float(data.get("output_tps_60") or 0.0), int(data.get("active_sessions") or 0),
                               forecast, forecast_tone, note)
        icon = getattr((getattr(app, "icons", None) or {}).get(provider), "icon", None)
        result.append((card, icon))
    return result


class SettingsController:
    """Keep Tk on one UI thread; tray callbacks only enqueue commands."""
    def __init__(self, app):
        self.app = app
        self.commands = queue.Queue()
        self.thread = None
        self.lock = threading.Lock()

    def show(self):
        with self.lock:
            self.commands.put(("show",))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="settings-ui", daemon=True)
                self.thread.start()

    def close(self):
        self.commands.put(("close",))

    def _run(self):
        SettingsWindow(self.app, self.commands).run()


class SettingsWindow:
    """Dark, panel-styled settings: a sidebar and five pages."""

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
        root.geometry(f"{S(920)}x{S(640)}")
        root.minsize(S(860), S(600))
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        families = set(tkfont.families(root))
        self.family = next((name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "Noto Sans CJK SC", "Segoe UI")
                            if name in families), "TkDefaultFont")
        self.images = {}              # keep PhotoImage references alive
        self.page = "overview"
        self.hover_card = None
        self.vars = {}
        self.origins = {}
        self._layout()

    # ----- small widgets -------------------------------------------------------------
    def font(self, size=10, weight="normal"):
        return (self.family, size, weight)

    def label(self, parent, text="", size=10, color=view.INK, weight="normal", **kw):
        return self.tk.Label(parent, text=text, bg=kw.pop("bg", parent["bg"]), fg=color, font=self.font(size, weight),
                             justify="left", anchor="w", **kw)

    def button(self, parent, text, command, kind="ghost"):
        tk = self.tk
        colors = {"primary": ("#7ec9a6", "#93d6b7", "#1f1e1c"), "ghost": (view.SURFACE_HI, "#3d3c36", view.INK),
                  "danger": ("#3a2524", "#4a2b29", "#ef8a7f")}
        base, hover, ink = colors[kind]
        widget = tk.Label(parent, text=text, bg=base, fg=ink, font=self.font(10, "bold" if kind == "primary" else "normal"),
                          padx=self.S(14), pady=self.S(6), cursor="hand2")
        widget.bind("<Enter>", lambda _: widget.configure(bg=hover))
        widget.bind("<Leave>", lambda _: widget.configure(bg=base))
        widget.bind("<Button-1>", lambda _: command())
        return widget

    def switch(self, parent, variable, command=None):
        tk, S = self.tk, self.S
        canvas = tk.Canvas(parent, width=S(40), height=S(22), bg=parent["bg"], highlightthickness=0, cursor="hand2")

        def draw(*_):
            canvas.delete("all")
            on = bool(variable.get())
            fill = "#7ec9a6" if on else "#4a4842"
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

    def row(self, parent, title, detail, variable, command=None):
        """A setting line: title + explanation on the left, switch on the right."""
        frame = self.tk.Frame(parent, bg=view.SURFACE, padx=self.S(16), pady=self.S(12))
        text = self.tk.Frame(frame, bg=view.SURFACE)
        text.pack(side="left", fill="x", expand=True)
        self.label(text, title, 11, weight="bold").pack(anchor="w")
        if detail:
            self.label(text, detail, 9, view.INK_3, wraplength=self.S(520)).pack(anchor="w", pady=(self.S(2), 0))
        self.switch(frame, variable, command).pack(side="right", padx=(self.S(12), 0))
        return frame

    def heading(self, parent, title, subtitle=""):
        self.label(parent, title, 16, weight="bold").pack(anchor="w")
        if subtitle:
            self.label(parent, subtitle, 10, view.INK_3, wraplength=self.S(640)).pack(anchor="w", pady=(self.S(2), self.S(14)))

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
        return inner

    # ----- layout ----------------------------------------------------------------------
    def _layout(self):
        tk, S = self.tk, self.S
        side = tk.Frame(self.root, bg=view.SIDEBAR, width=S(200))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=view.SIDEBAR)
        brand.pack(fill="x", padx=S(18), pady=(S(22), S(26)))
        dots = tk.Canvas(brand, width=S(30), height=S(30), bg=view.SIDEBAR, highlightthickness=0)
        for index, provider in enumerate(("codex", "gemini", "mimo")):
            color = view.hex_color(view.accent(letter_of(provider)))
            x = S(4 + index * 9)
            dots.create_oval(x, S(9), x + S(12), S(21), fill=color, outline=view.SIDEBAR, width=S(2))
        dots.pack(side="left")
        names = tk.Frame(brand, bg=view.SIDEBAR)
        names.pack(side="left", padx=(S(8), 0))
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
        self.label(side, "托盘右键或面板齿轮可打开此页", 8, view.INK_3, wraplength=S(170)).pack(side="bottom", anchor="w", padx=S(20), pady=S(18))

        main = tk.Frame(self.root, bg=view.BG)
        main.pack(side="left", fill="both", expand=True)
        footer = tk.Frame(main, bg=view.BG)
        footer.pack(side="bottom", fill="x", padx=S(28), pady=(S(6), S(18)))
        self.status = tk.StringVar()
        self.label(footer, "", 10, view.INK_2, textvariable=self.status).pack(side="left")
        self.button(footer, "保存并应用", self.save, "primary").pack(side="right")
        self.button(footer, "关闭", self.root.withdraw).pack(side="right", padx=S(8))
        self.body = tk.Frame(main, bg=view.BG)
        self.body.pack(fill="both", expand=True, padx=S(28), pady=(S(24), 0))
        self.pages = {key: tk.Frame(self.body, bg=view.BG) for key, _ in PAGES}
        for frame in self.pages.values():
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._overview(self.scrollable(self.pages["overview"]))
        self._sources(self.scrollable(self.pages["sources"]))
        self._agents(self.scrollable(self.pages["agents"]))
        self._naming(self.pages["naming"])
        self._general(self.pages["general"])
        self.show_page("overview")

    def show_page(self, key):
        self.page = key
        for name, (item, bar, text) in self.nav.items():
            on = name == key
            item.configure(bg=view.SURFACE if on else view.SIDEBAR)
            text.configure(bg=view.SURFACE if on else view.SIDEBAR, fg=view.INK if on else view.INK_2)
            bar.configure(bg="#7ec9a6" if on else view.SIDEBAR)
        self.pages[key].tkraise()
        self.refresh_live()

    # ----- pages -------------------------------------------------------------------------
    def _overview(self, page):
        tk, S = self.tk, self.S
        top = tk.Frame(page, bg=view.BG)
        top.pack(fill="x")
        left = tk.Frame(top, bg=view.BG)
        left.pack(side="left")
        self.heading(left, "概览", "每张卡片与托盘图标同色：额度条、TPS 与图标保持一致。点击卡片查看额度详情。")
        self.button(top, "立即刷新", self.app.request_refresh).pack(side="right", anchor="n")
        self.card_grid = tk.Frame(page, bg=view.BG)
        self.card_grid.pack(fill="x")
        self.chart_holder = tk.Frame(page, bg=view.BG)
        self.chart_holder.pack(fill="x", pady=(S(14), 0))

    def _sources(self, page):
        tk, S = self.tk, self.S
        self.heading(page, "额度来源", "各数据源的接入状态。Claude 需要订阅后再开启；MiMo 通过控制台登录读取。")
        self.vars["claude"] = tk.BooleanVar()
        self.row(page, "Claude 额度检测", "开启后读取 Claude Code 的订阅额度；更改此项需要重启托盘（保存时自动重启）。",
                 self.vars["claude"]).pack(fill="x", pady=(0, S(10)))
        actions = tk.Frame(page, bg=view.BG)
        actions.pack(fill="x", pady=(0, S(14)))
        self.button(actions, "登录小米 MiMo", self.app.mimo_login).pack(side="left")
        if "mimo" in self.app.snapshots:
            self.button(actions, "退出 MiMo 登录", self.app.mimo_logout).pack(side="left", padx=S(8))
        self.button(actions, "全部 AI 速率", self.app.open_all_performance).pack(side="left", padx=S(8))
        self.label(page, "本机记录", 11, weight="bold").pack(anchor="w", pady=(0, S(6)))
        self.health_box = tk.Frame(page, bg=view.BG)
        self.health_box.pack(fill="x")

    def _agents(self, page):
        tk, S = self.tk, self.S
        self.heading(page, "任务与 herdr", "任务气泡来自本机 agent 记录；使用 herdr 时，同一窗格的会话合并为一个气泡。")
        self.vars["herdr"] = tk.BooleanVar(value=True)
        self.vars["order"] = tk.BooleanVar(value=True)
        self.herdr_row = self.row(page, "herdr 适配", "", self.vars["herdr"])
        self.herdr_row.pack(fill="x", pady=(0, S(8)))
        self.herdr_detail = self.herdr_row.winfo_children()[0].winfo_children()
        self.row(page, "图标自动排序", "恢复时间近的图标排在左侧，用完的排在最后；关闭后按固定顺序显示。",
                 self.vars["order"]).pack(fill="x", pady=(0, S(14)))
        self.agent_holder = tk.Frame(page, bg=view.BG)
        self.agent_holder.pack(fill="x")

    def _naming(self, page):
        tk, S = self.tk, self.S
        self.heading(page, "任务命名 API", "可选：用 OpenAI 兼容接口（含 MiMo Token Plan）把请求归纳为气泡标题。密钥只保存在本机。")
        self.vars["summary"] = tk.BooleanVar()
        self.row(page, "启用模型命名", "启用后发送每个成员最近一次请求的前 400 字、项目文件夹名和当前动作。",
                 self.vars["summary"]).pack(fill="x", pady=(0, S(14)))
        self.fields = {name: tk.StringVar() for name in ("url", "key", "model")}
        form = tk.Frame(page, bg=view.SURFACE, padx=S(16), pady=S(12))
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        for index, (name, title, hint) in enumerate((("url", "API 地址", "https://…/v1"), ("key", "API Key", "仅保存在本机"),
                                                     ("model", "模型", "例如 mimo-v2-flash"))):
            self.label(form, title, 10, view.INK_2).grid(row=index, column=0, sticky="w", pady=S(7), padx=(0, S(16)))
            entry = tk.Entry(form, textvariable=self.fields[name], show="●" if name == "key" else "", bg=view.FIELD, fg=view.INK,
                             insertbackground=view.INK, relief="flat", font=self.font(10), highlightthickness=1,
                             highlightbackground=view.BORDER, highlightcolor="#7ec9a6")
            entry.grid(row=index, column=1, sticky="ew", pady=S(7), ipady=S(5))
            self.label(form, hint, 9, view.INK_3).grid(row=index, column=2, sticky="w", padx=(S(12), 0))
        test = tk.Frame(page, bg=view.BG)
        test.pack(fill="x", pady=(S(12), 0))
        self.test_button = self.button(test, "测试连接", self.test_connection)
        self.test_button.pack(side="left")
        self.result = tk.StringVar()
        self.label(test, "", 10, view.INK_2, textvariable=self.result).pack(side="left", padx=S(12))
        if any(os.environ.get("CODEX_QUOTA_SUMMARY_" + name) for name in ("URL", "KEY", "MODEL")):
            self.label(page, "已设置 CODEX_QUOTA_SUMMARY_* 环境变量，环境变量优先于本页配置。", 9, "#e5a84b").pack(anchor="w", pady=(S(12), 0))

    def _general(self, page):
        tk, S = self.tk, self.S
        self.heading(page, "常规", "启动、诊断与退出。")
        self.vars["startup"] = tk.BooleanVar()
        self.row(page, "开机自启", "登录 Windows 后自动在托盘显示。", self.vars["startup"]).pack(fill="x", pady=(0, S(14)))
        tools = tk.Frame(page, bg=view.SURFACE, padx=S(16), pady=S(14))
        tools.pack(fill="x")
        self.label(tools, "工具", 11, weight="bold").pack(anchor="w", pady=(0, S(10)))
        line = tk.Frame(tools, bg=view.SURFACE)
        line.pack(fill="x")
        self.button(line, "诊断信息", self.app.open_diagnostics).pack(side="left")
        self.button(line, "全局重置预测", self.app.open_reset_page).pack(side="left", padx=S(8))
        self.button(line, "打开数据文件夹", self.open_data_folder).pack(side="left")
        danger = tk.Frame(page, bg=view.SURFACE, padx=S(16), pady=S(14))
        danger.pack(fill="x", pady=(S(14), 0))
        text = tk.Frame(danger, bg=view.SURFACE)
        text.pack(side="left", fill="x", expand=True)
        self.label(text, "退出", 11, weight="bold").pack(anchor="w")
        self.label(text, "关闭全部托盘图标与后台读取。", 9, view.INK_3).pack(anchor="w")
        self.button(danger, "退出全部", self.app.exit_app, "danger").pack(side="right")

    # ----- live content --------------------------------------------------------------------
    def refresh_live(self):
        if self.root.state() == "withdrawn":
            return
        try:
            if self.page == "overview":
                self._fill_overview()
            elif self.page == "sources":
                self._fill_health()
            elif self.page == "agents":
                self._fill_agents()
        except Exception:
            logging.getLogger(__name__).exception("settings refresh failed")

    def _fill_overview(self):
        S = self.S
        width = self.body.winfo_width() or S(660)
        columns = 2 if width >= S(view.CARD_W * 2 + 14) else 1
        cards = build_cards(self.app)
        keys = [card.provider for card, _ in cards]
        if self.images.get("overview:keys") != (keys, columns):
            for child in self.card_grid.winfo_children():
                child.destroy()
            for key in list(self.images):
                if key.startswith("card:"):
                    self.images.pop(key)
            self.images["overview:keys"] = (keys, columns)
        for index, (card, icon) in enumerate(cards):
            key = "card:" + card.provider
            image = view.source_card(card, icon, self.scale, hover=self.hover_card == card.provider)
            widget = self.picture(self.card_grid, key, image, cursor="hand2")
            if not widget.grid_info():
                widget.grid(row=index // columns, column=index % columns, padx=(0, S(14)), pady=(0, S(14)), sticky="nw")
                widget.bind("<Button-1>", lambda _, p=card.provider: self.app.open_details(p))
                widget.bind("<Enter>", lambda _, p=card.provider: self._hover(p))
                widget.bind("<Leave>", lambda _: self._hover(None))
        events = []
        try:
            events = list(self.app.tps_monitor.events)
        except (AttributeError, RuntimeError):
            pass
        letters = {provider: letter_of(provider) for provider in self.app.snapshots}
        chart_width = columns * view.CARD_W + (columns - 1) * 14
        chart = view.activity_chart(events, time.time(), letters, self.scale, width=max(view.CARD_W, chart_width))
        widget = self.picture(self.chart_holder, "chart", chart)
        if not widget.winfo_manager():
            widget.pack(anchor="w")

    def _hover(self, provider):
        self.hover_card = provider
        self._fill_overview()

    def _fill_health(self):
        tk, S = self.tk, self.S
        for child in self.health_box.winfo_children():
            child.destroy()
        health = (getattr(self.app, "performance", None) or {}).get("source_health", {}) or {}
        tones = {"ok": ("#7ec9a6", "#23342c"), "error": ("#ef8a7f", "#3a2524"), "idle": (view.INK_3, view.SURFACE_HI)}
        row = None
        for index, (name, value) in enumerate(sorted(health.items())):
            if index % 3 == 0:
                row = tk.Frame(self.health_box, bg=view.BG)
                row.pack(fill="x", pady=(0, S(8)))
            status = str((value or {}).get("status") or "未知")
            ink, fill = tones[view.health_tone(status)]
            chip = tk.Frame(row, bg=view.SURFACE, padx=S(12), pady=S(9), width=S(206), height=S(58))
            chip.pack(side="left", padx=(0, S(8)))
            chip.pack_propagate(False)
            self.label(chip, name, 10, weight="bold").pack(anchor="w")
            extra = value.get("sessions") if isinstance(value, dict) else None
            line = tk.Frame(chip, bg=view.SURFACE)
            line.pack(anchor="w", pady=(S(3), 0))
            tk.Label(line, text=" " + status + " ", bg=fill, fg=ink, font=self.font(8)).pack(side="left")
            if extra is not None:
                self.label(line, f"  {extra} 个会话", 8, view.INK_3).pack(side="left")
        if not health:
            self.label(self.health_box, "正在收集…", 10, view.INK_3).pack(anchor="w")

    def _fill_agents(self):
        herdr = getattr(getattr(self.app, "tps_monitor", None), "herdr", None)
        if herdr is not None and len(self.herdr_detail) == 1:
            self.herdr_detail.append(self.label(self.herdr_row.winfo_children()[0], "", 9, view.INK_3, wraplength=self.S(520)))
            self.herdr_detail[-1].pack(anchor="w", pady=(self.S(2), 0))
        if herdr is not None:
            env = os.environ.get("CODEX_QUOTA_HERDR")
            text = f"状态：{herdr.status} · {len(herdr.rows)} 个窗格。同一窗格的会话合并为一个气泡，blocked 显示为“等你确认”。"
            if env:
                text += f"（环境变量 CODEX_QUOTA_HERDR={env} 优先）"
            self.herdr_detail[-1].configure(text=text)
        letters = {provider: letter_of(provider) for provider in ("codex", "grok", "gemini", "claude", "mimo")}
        agents = [agent for agent in (getattr(self.app, "agents", None) or []) if not agent.get("schedule")]
        width = round((self.body.winfo_width() or self.S(660)) / self.scale)
        widget = self.picture(self.agent_holder, "agents", view.agent_rows(agents, letters, self.scale, width=max(400, width)))
        if not widget.winfo_manager():
            widget.pack(anchor="w")

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
        self.vars["claude"].set("claude" in providers.enabled_keys())
        herdr = getattr(getattr(self.app, "tps_monitor", None), "herdr", None)
        self.vars["herdr"].set(bool(herdr.enabled) if herdr is not None else True)
        import sys
        order = getattr(sys.modules.get("tray_app") or sys.modules.get("__main__"), "TRAY_ORDER_MODE", "auto")
        self.vars["order"].set(order != "fixed")
        self.origins = {name: var.get() for name, var in self.vars.items()}
        self.status.set("")
        self.result.set("")

    def save(self):
        try:
            self.app.apply_summary_settings({name: var.get() for name, var in self.fields.items()}, self.vars["summary"].get())
            if self.vars["startup"].get() != self.origins["startup"]:
                self.app.toggle_startup()
            prefs = {"herdr": bool(self.vars["herdr"].get()), "tray_order": "auto" if self.vars["order"].get() else "fixed"}
            if hasattr(self.app, "save_preferences"):
                self.app.save_preferences(prefs)
            self.status.set("✓ 设置已保存并生效")
            if self.vars["claude"].get() != self.origins["claude"]:
                self.root.withdraw()
                self.app.toggle_claude()
            self.origins = {name: var.get() for name, var in self.vars.items()}
        except (OSError, ValueError) as error:
            self.messagebox.showerror("未能保存", str(error), parent=self.root)

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
                    self.root.after(60, self.refresh_live)
                elif command[0] == "result":
                    self.result.set(command[1])
            self.ticks += 1
            if self.ticks % 20 == 0:          # live figures every 2 s
                self.refresh_live()
            self.root.after(100, poll)

        poll()
        self.root.mainloop()
