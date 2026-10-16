# AI 额度托盘

Windows 通知区域程序，显示 Codex、Grok 和 Gemini（5 小时窗口）的账号额度、重置倒计时和更新时间。启动时立即显示上次缓存的额度，再在后台并行刷新；刷新按需调度（见下文），也支持托盘菜单立即刷新；刷新失败时保留最后有效值并标记“旧数据”。

## 当前数据源

- Codex：调用本机 Codex App Server 的 `account/rateLimits/read`，同时读取 5 小时主窗口和每周次窗口；图标外环按更紧张的窗口显示，详情分别列出两个窗口。
- Grok：每次刷新启动临时 `grok` 终端，等待会话就绪后执行 `/usage`，读取 `Weekly limit` 和 `Next reset`。启动较慢时在 25 秒内重试该查询，退出时关闭临时进程。
- Gemini（5 小时）：每次刷新调用 `agy --print /usage --output-format json`，读取 `gemini-5h` 桶的剩余额度和重置时间。

- Claude（已做好接入准备，检测到 Claude Code 时自动显示，图标字母 A、陶土色）：每次刷新在应用自有的空文件夹 `%LOCALAPPDATA%\CodexQuotaTray\claude-probe` 中启动临时 `claude --strict-mcp-config` 会话并执行 `/usage`，读取 “Current session”（5 小时，主窗口）和 “Current week (all models)”（每周，次窗口）的已用百分比与重置时间；首次在该文件夹运行时自动确认“信任此文件夹”。API Key 计费账号没有订阅额度，会显示明确的提示。

程序不会读取或输出 `auth.json`、token、cookie、账号 ID 等凭据或身份信息。

## 使用

需要 Windows 10/11、Python 3.12（64 位），以及已安装并登录的对应 AI 客户端。克隆仓库后，可安装依赖并直接从源码启动：

```powershell
python -m pip install -r requirements.txt
python -X utf8 tray_app.py
```

直接运行 `outputs\CodexQuotaTray.exe`。通知区域使用三个图标：C=GPT/Codex（鼠尾草绿）、G=Grok（浅银）、M=Gemini（长春花紫），以及检测到 Claude Code 时的 A=Claude（陶土色）。三种状态统一为铺满托盘格位的大圆盘：空闲时为暖炭色圆盘，中间是象牙白字母，外圈细环从 12 点方向顺时针表示剩余额度；检测到对应模型正在请求或生成时，同一圆盘点亮为该模型主色，并以深色数字显示 TPS。暗盘/亮盘的变化在 16 px 下也能一眼区分“空闲/生成中”。字母和数字统一使用衬线字体（优先 Windows 自带的 Sitka Small，依次回退到 Cambria、Palatino Linotype、Georgia）。刚结束生成的数字保留约 3 秒后恢复额度，避免快速闪烁。左键点击图标打开对应程序：C 打开 Codex 桌面，G 新开 Grok CLI 窗口，M 打开 Antigravity。右键菜单仍可刷新和查看详情。

外圈弧长表示可用额度，剩余不足 50% 时变为琥珀色，不足 20% 时变为玫瑰红（陶土色留给 Claude）；未知项只显示灰色空环和 `?`，旧数据右上角有带留白的琥珀圆点。图标在 256 px 画布上绘制后统一缩放。鼠标停在图标上约 0.4 秒后，独立置顶详情显示最近生成 TPS、来源、最近 60 秒输出速率、活动会话、额度重置日期、时区及倒计时。浮层不激活窗口、不抢键盘焦点，支持普通全屏/无边框窗口；移开鼠标后关闭，数据刷新不会打断显示。提示按图标所在显示器定位，支持负坐标。独占全屏或安全桌面无法保证覆盖；全屏隐藏任务栏时需先让托盘图标可见。“额度详情”和“本机全部 AI 速率”仍可从右键菜单打开。

## 接入新的模型 / 选择显示哪些

所有来源登记在 `providers.py` 的 `SPECS` 表中（键、图标字母、名称、主色、刷新间隔、打开方式）；托盘图标、菜单、刷新调度、颜色都由这张表生成。新增来源只需在表中加一项，并在 `quota_sources.py` 中提供额度读取函数（可选：在 `tps_sources.py` 中加入本地 token 记录读取）。

- 默认显示 Codex、Grok、Gemini；Claude 在找到 `claude.exe`/`claude.cmd` 或 `~\.claude\projects` 时自动显示。
- 环境变量 `CODEX_QUOTA_PROVIDERS=codex,grok,gemini,claude` 可指定要显示的来源；`CODEX_QUOTA_CLAUDE_EXE` 可指定 Claude Code 路径，`CLAUDE_CONFIG_DIR` 与 Claude Code 自身的设置一致。
- `CodexQuotaTray.exe --check-claude <结果文件.json> [--raw]` 单独检查 Claude 额度读取；加 `--raw` 时另存一份去除邮箱的 `/usage` 屏幕文本，便于 Claude Code 更新界面后核对解析规则。

## 按需刷新

不再固定每 5 分钟轮询，而是由 `refresh_scheduler.py` 按状态决定下一次查询：

| 状态 | Codex / Gemini | Grok / Claude |
| --- | --- | --- |
| 空闲 | 15 分钟 | 30 分钟 |
| 正在生成 | 3 分钟 | 10 分钟 |
| 一次生成结束后 | 8 秒后 | 20 秒后 |
| 读取失败 | 30 秒 → 1 分 → 2 分 → 5 分退避 | 同左 |

到达重置时间后 5 秒、电脑从睡眠唤醒（时钟跳变）以及手动“立即刷新”都会马上刷新。Grok 需要启动临时终端，成本较高，因此间隔更长。详情卡片底部显示每个来源的下一次计划刷新时间。

## 额度耗尽预测

每次成功读取的剩余额度会记录到 `%LOCALAPPDATA%\CodexQuotaTray\history.json`（仅数值和时间）。程序取当前重置窗口内最近 3 小时、跨度至少 10 分钟的样本做最小二乘拟合，估算消耗速度：

- 预计在重置前耗尽：显示“约 X 后耗尽”，并按剩余时间标记琥珀色/玫瑰红；
- 足够撑到重置：显示“可撑到重置”；
- 样本不足或近期没有消耗：显示“样本不足”或“近期无消耗”。

额度回升（窗口重置）时自动截断旧样本。预测结果出现在详情卡片、右键菜单和“额度详情”中。

## 详情卡片

悬停详情改为自绘卡片（`detail_card.py` 渲染、`hover_details.py` 显示）：分层（layered）、鼠标穿透、不激活、置顶窗口，带圆角、阴影和淡入。卡片包含：

- 主数字：当前约束窗口的剩余百分比与重置倒计时；
- 各窗口进度条（Codex 显示 5 小时 + 每周）；
- 耗尽预测一行；
- TPS、60 秒输出速率、活动会话等指标；
- 底部：各窗口重置时间、下一次计划刷新。

卡片按图标所在显示器的 DPI 渲染（悬停线程启用 Per-Monitor V2）；若遇到旧系统显示异常，可设置环境变量 `CODEX_QUOTA_LEGACY_DPI=1` 关闭。卡片创建失败时自动回退到原有文字提示。

## TPS 数据来源和含义

程序每秒读取一次已有本地记录，独立于额度刷新，不发送模型请求。只跟踪 24 小时内修改过的会话文件，空闲读取器会被回收；Grok `usage.json` 仅在修改时间或大小变化时重新解析。

- GPT/Codex：读取本机近期会话的真实 `token_usage_record.output_tokens`，按会话关联客户端 `codex_core::stream_events_utils` 的首个输出项时间，以“真实输出 token 数 ÷ 首个输出项到计数完成的流式时间”统计 TPS。计数包含思考和工具调用输出，不计工具执行时间；完成时结算，不使用字符长度、近似分词器或请求整体耗时。流式日志跨度里的 `model=` 用于识别尚未写入线程表的 GPT 会话，避免活动被归到“其他”而 C 图标不显示数字。没有会话 ID 的 UI delta 日志不用于跨会话关联。老版本 `token_count` 只用于去重后的 60 秒输出计数。
- Grok CLI：直接读取客户端 `unified.jsonl` 中 `shell.turn.inference_done` 的原生 `tokens_per_sec`，保持客户端数值；用 `events.jsonl` 判断活动，并读取 `updates.jsonl` 和 `usage.json` 的真实输出计数。
- Gemini/Antigravity 桌面及 CLI：只读读取会话 SQLite 的 generation/step 数值字段，对输出总量与“回复输出+思考输出”做一致性检查。目前本机客户端没有找到原生 TPS 数字，因此保留额度显示，只展示真实计数的 60 秒输出速率，不用耗时推算生成 TPS。
- Claude Code：只读读取 `~\.claude\projects` 下近期会话（含子代理）记录中每条回复的 `message.usage.output_tokens`，同一回复分块写入时只按最终计数统计一次，不计输入和缓存，不保存正文。Claude Code 没有提供原生 TPS，因此和 Gemini 一样只显示 60 秒输出速率与活动状态，不用耗时推算 TPS。
- Codex 的 Gemini 桥接器：监听现有临时隔离配置中的会话记录，保存数值投影；对已识别的桥接回传与底层生成做一对一去重，无法观察底层时保留回传计数。

使用时显示最近一次完整的生成 TPS，样本超过 5 分钟后失效；详情注明来源和样本时间。客户端在响应完成后发布真实 token 计数，因此尚未取得完整样本时继续显示额度。60 秒输出速率为所有相应会话在窗口内报告的输出 token 之和除以 60，不计输入和缓存，不重复加入已包含的思考 token。它作为独立指标显示在详情中，不充当生成 TPS。没有记录不证明没有生成。

原始对话、响应正文、账号身份和凭据不会写入 TPS 状态。数值状态为 `%LOCALAPPDATA%\CodexQuotaTray\performance.json`。未提供可核验本地 token 信号的软件、独立 AI 网页和其他电脑的使用不能由这个本地观察器自动读取，详情会列明可观测范围。

悬停期间保持托盘图标稳定，详情继续读取每秒更新的数据；移开后图标恢复最新数值。`CodexQuotaTray.exe --check-hover <结果文件.json>` 可进行约 4 秒的置顶全屏覆盖、连续刷新稳定性和焦点保持检查，检查画面会自动关闭。

读取方案参考 [Tokscale](https://github.com/junhoyeo/tokscale)、[Tallyburn](https://github.com/seoulpro/tallyburn)、[Grok Build](https://github.com/xai-org/grok-build) 及 [Codex 官方遥测说明](https://learn.chatgpt.com/docs/config-file/config-advanced)。程序使用独立的 Python 数值读取器，不运行这些项目的守护进程。

## 开机启动与日志

“开机启动”写入 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`（旧版启动文件夹中的 `.cmd` 会自动迁移）。运行日志为 `%LOCALAPPDATA%\CodexQuotaTray\app.log`（自动轮转），诊断文件也保存在该目录。

## 构建与测试

```powershell
python -X utf8 -m unittest -v test_quota_sources.py test_tps_sources.py test_tray_app.py test_features.py test_claude.py
.\build.ps1 -DistPath .\outputs
```

上述命令输出单文件、无控制台的 `outputs\CodexQuotaTray.exe`；不指定 `-DistPath` 时，维护脚本沿用本机部署约定，将 EXE 写入仓库的上一级目录。构建时的临时文件保存在 `work`，单文件程序的依赖解包到仓库上一级的 `runtime-temp`。若将 `CODEX_QUOTA_CODEX_EXE` 设置为 Codex 可执行文件路径，程序会优先使用该路径。

可用 `CodexQuotaTray.exe --check-grok <结果文件.json>` 单独检查打包后的 Grok 真实读取链路，成功退出码为 0；结果文件仅包含额度字段。程序在缺少标准句柄时初始化隐藏终端，并在查询结束后释放。打包显式包含 pywinpty 原生组件。替换 EXE 前应退出正在运行的旧版。

`CodexQuotaTray.exe --check-tps <结果文件.json>` 读取两次本机真实数据后退出，用于验证打包后的 TPS 读取器。
