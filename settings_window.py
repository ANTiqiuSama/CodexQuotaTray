"""User-opened settings window; secrets stay in the local data directory."""
from __future__ import annotations

import json
import os
import queue
import threading
from urllib.parse import urlsplit

import agent_tasks


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
        import tkinter as tk
        from tkinter import ttk, messagebox

        root = tk.Tk()
        root.withdraw()
        root.title("CodexQuotaTray 设置")
        root.geometry("700x670")
        root.minsize(660, 640)
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TNotebook.Tab", padding=(14, 6))
        book = ttk.Notebook(root)
        book.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        general = ttk.Frame(book, padding=18)
        naming = ttk.Frame(book, padding=18)
        book.add(general, text="常规与额度")
        book.add(naming, text="任务命名 API")
        general.columnconfigure(0, weight=1)
        naming.columnconfigure(1, weight=1)

        startup = tk.BooleanVar()
        claude = tk.BooleanVar()
        enabled = tk.BooleanVar()
        fields = {name: tk.StringVar() for name in ("url", "key", "model")}
        state = tk.StringVar()
        result = tk.StringVar()
        origins = {}
        ttk.Label(general, text="本机 AI 额度", font=("Microsoft YaHei UI", 12, "bold")).grid(sticky="w")
        ttk.Label(general, textvariable=state, justify="left", wraplength=550).grid(sticky="w", pady=(12, 18))
        ttk.Checkbutton(general, text="开机自启", variable=startup).grid(sticky="w", pady=5)
        ttk.Checkbutton(general, text="Claude 额度检测（订阅开通后启用）", variable=claude).grid(sticky="w", pady=5)
        ttk.Label(general, text="Claude 关闭时不读取订阅额度；更改此项后托盘会重启。", wraplength=540).grid(sticky="w", pady=(0, 18))
        actions = ttk.Frame(general)
        actions.grid(sticky="w")
        ttk.Button(actions, text="立即刷新额度", command=self.app.request_refresh).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="登录小米 MiMo", command=self.app.mimo_login).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="诊断信息", command=self.app.open_diagnostics).pack(side="left")
        details = ttk.Frame(general)
        details.grid(sticky="w", pady=10)
        source = tk.StringVar(value=next(iter(self.app.snapshots)))
        ttk.Combobox(details, textvariable=source, values=list(self.app.snapshots), state="readonly", width=9).pack(side="left", padx=(0, 8))
        ttk.Button(details, text="额度详情", command=lambda: self.app.open_details(source.get())).pack(side="left", padx=(0, 8))
        ttk.Button(details, text="全部 AI 速率", command=self.app.open_all_performance).pack(side="left", padx=(0, 8))
        ttk.Button(details, text="全局重置预测", command=self.app.open_reset_page).pack(side="left")
        if "mimo" in self.app.snapshots:
            ttk.Button(general, text="退出 MiMo 登录", command=self.app.mimo_logout).grid(sticky="w", pady=4)
        ttk.Label(general, text="托盘右键或面板右下角齿轮均可打开此页。\n右键合并后的大气泡仍可拆分任务。", wraplength=540).grid(sticky="w", pady=24)

        ttk.Checkbutton(naming, text="启用模型为任务气泡命名", variable=enabled).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        entries = {}
        for row, (name, label) in enumerate((("url", "API Base URL"), ("key", "API Key"), ("model", "模型")), 1):
            ttk.Label(naming, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=8)
            entry = ttk.Entry(naming, textvariable=fields[name], show="●" if name == "key" else "")
            entry.grid(row=row, column=1, sticky="ew", pady=8)
            entries[name] = entry
        ttk.Label(naming, text="支持 MiMo Token Plan 与兼容 Chat Completions 的 API。\n启用后会发送任务请求摘要与最近动作，用于生成任务名。\n密钥仅保存在本机，输入框始终隐藏密钥。", wraplength=540, justify="left").grid(row=4, column=0, columnspan=2, sticky="w", pady=16)
        env_override = any(os.environ.get("CODEX_QUOTA_SUMMARY_" + name) for name in ("URL", "KEY", "MODEL"))
        if env_override:
            ttk.Label(naming, text="当前设置了 SUMMARY 环境变量，环境变量优先于本页配置。", wraplength=540).grid(row=5, column=0, columnspan=2, sticky="w")

        def test_connection():
            config = {name: value.get().strip() for name, value in fields.items()}
            address = urlsplit(config["url"])
            if address.scheme not in ("http", "https") or not address.hostname or not config["model"]:
                result.set("请先填写 API 地址与模型")
                return
            config["url"] = config["url"].rstrip("/")
            if not config["url"].endswith("/chat/completions"):
                config["url"] += "/chat/completions"
            test_button.configure(state="disabled")
            result.set("正在测试连接…")

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

        test_button = ttk.Button(naming, text="测试连接", command=test_connection)
        test_button.grid(row=6, column=0, sticky="w", pady=12)
        ttk.Label(naming, textvariable=result, wraplength=420).grid(row=6, column=1, sticky="w", padx=8)
        status = tk.StringVar()
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=20, pady=(0, 16))
        ttk.Label(bottom, textvariable=status).pack(side="left")

        def load():
            import providers
            config = load_summary()
            for name, value in fields.items():
                value.set(config[name])
            enabled.set(config["enabled"])
            startup.set(self.app.startup_enabled())
            claude.set("claude" in providers.enabled_keys())
            origins.update(startup=startup.get(), claude=claude.get())
            state.set("\n".join(self.app.menu_quota(p) + "\n" + self.app.menu_reset(p) for p in self.app.snapshots))
            status.set("")
            result.set("")

        def save():
            try:
                self.app.apply_summary_settings({name: var.get() for name, var in fields.items()}, enabled.get())
                if startup.get() != origins["startup"]:
                    self.app.toggle_startup()
                    origins["startup"] = startup.get()
                status.set("设置已保存并生效")
                if claude.get() != origins["claude"]:
                    root.withdraw()
                    self.app.toggle_claude()
            except (OSError, ValueError) as error:
                messagebox.showerror("未能保存", str(error), parent=root)

        ttk.Button(bottom, text="保存并应用", command=save).pack(side="right")
        ttk.Button(bottom, text="关闭", command=root.withdraw).pack(side="right", padx=8)
        ttk.Button(general, text="退出全部", command=self.app.exit_app).grid(sticky="w", pady=8)

        def poll():
            if self.app.stop_event.is_set():
                root.destroy()
                return
            while True:
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    break
                if command[0] == "close":
                    root.destroy()
                    return
                if command[0] == "show":
                    if root.state() == "withdrawn":
                        load()
                    root.deiconify()
                    root.lift()
                elif command[0] == "result":
                    result.set(command[1])
                    test_button.configure(state="normal")
            root.after(100, poll)

        poll()
        root.mainloop()
