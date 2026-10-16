# AI 额度托盘

Windows 通知区域程序，显示 Codex、Grok 和 Gemini（5 小时窗口）的账号额度、重置倒计时和更新时间。程序每 5 分钟刷新，也支持托盘菜单立即刷新；刷新失败时保留最后有效值并标记“旧数据”。

## 当前数据源

- Codex：调用本机 Codex App Server 的 `account/rateLimits/read`，读取真实账号额度。
- Grok：每次刷新启动临时 `grok` 终端，等待会话就绪后执行 `/usage`，读取 `Weekly limit` 和 `Next reset`。启动较慢时在 25 秒内重试该查询，退出时关闭临时进程。
- Gemini（5 小时）：每次刷新调用 `agy --print /usage --output-format json`，读取 `gemini-5h` 桶的剩余额度和重置时间。

程序不会读取或输出 `auth.json`、token、cookie、账号 ID 等凭据或身份信息。

## 使用

需要 Windows 10/11、Python 3.12（64 位），以及已安装并登录的对应 AI 客户端。克隆仓库后，可安装依赖并直接从源码启动：

```powershell
python -m pip install -r requirements.txt
python -X utf8 tray_app.py
```

直接运行 `outputs\CodexQuotaTray.exe`。通知区域使用三个图标：C=GPT/Codex（鼠尾草绿）、G=Grok（浅银）、M=Gemini（长春花紫）。空闲时为暖炭色圆盘：中间是象牙白字母，外圈细环从 12 点方向顺时针表示剩余额度；检测到对应模型正在请求或生成时，圆盘变为该模型主色的圆角方块，并以深色数字显示 TPS。形状变化在 16 px 下也能一眼区分“空闲/生成中”。刚结束生成的数字保留约 3 秒后恢复额度，避免快速闪烁。左键点击图标打开对应程序：C 打开 Codex 桌面，G 新开 Grok CLI 窗口，M 打开 Antigravity。右键菜单仍可刷新和查看详情。

外圈弧长表示可用额度，剩余不足 50% 时变为琥珀色，不足 20% 时变为陶土红；未知项只显示灰色空环和 `?`，旧数据右上角有带留白的琥珀圆点。图标在 256 px 画布上绘制后统一缩放，数字优先使用 Windows 自带的 Bahnschrift 窄体。鼠标停在图标上约 0.4 秒后，独立置顶详情显示最近生成 TPS、来源、最近 60 秒输出速率、活动会话、额度重置日期、时区及倒计时。浮层不激活窗口、不抢键盘焦点，支持普通全屏/无边框窗口；移开鼠标后关闭，数据刷新不会打断显示。提示按图标所在显示器定位，支持负坐标。独占全屏或安全桌面无法保证覆盖；全屏隐藏任务栏时需先让托盘图标可见。“额度详情”和“本机全部 AI 速率”仍可从右键菜单打开。

## TPS 数据来源和含义

程序每秒读取一次已有本地记录，独立于每 5 分钟的额度刷新，不发送模型请求。

- GPT/Codex：读取本机近期会话的真实 `token_usage_record.output_tokens`，按会话关联客户端 `codex_core::stream_events_utils` 的首个输出项时间，以“真实输出 token 数 ÷ 首个输出项到计数完成的流式时间”统计 TPS。计数包含思考和工具调用输出，不计工具执行时间；完成时结算，不使用字符长度、近似分词器或请求整体耗时。流式日志跨度里的 `model=` 用于识别尚未写入线程表的 GPT 会话，避免活动被归到“其他”而 C 图标不显示数字。没有会话 ID 的 UI delta 日志不用于跨会话关联。老版本 `token_count` 只用于去重后的 60 秒输出计数。
- Grok CLI：直接读取客户端 `unified.jsonl` 中 `shell.turn.inference_done` 的原生 `tokens_per_sec`，保持客户端数值；用 `events.jsonl` 判断活动，并读取 `updates.jsonl` 和 `usage.json` 的真实输出计数。
- Gemini/Antigravity 桌面及 CLI：只读读取会话 SQLite 的 generation/step 数值字段，对输出总量与“回复输出+思考输出”做一致性检查。目前本机客户端没有找到原生 TPS 数字，因此保留额度显示，只展示真实计数的 60 秒输出速率，不用耗时推算生成 TPS。
- Codex 的 Gemini 桥接器：监听现有临时隔离配置中的会话记录，保存数值投影；对已识别的桥接回传与底层生成做一对一去重，无法观察底层时保留回传计数。

使用时显示最近一次完整的生成 TPS，样本超过 5 分钟后失效；详情注明来源和样本时间。客户端在响应完成后发布真实 token 计数，因此尚未取得完整样本时继续显示额度。60 秒输出速率为所有相应会话在窗口内报告的输出 token 之和除以 60，不计输入和缓存，不重复加入已包含的思考 token。它作为独立指标显示在详情中，不充当生成 TPS。没有记录不证明没有生成。

原始对话、响应正文、账号身份和凭据不会写入 TPS 状态。数值状态为 `%LOCALAPPDATA%\CodexQuotaTray\performance.json`。未提供可核验本地 token 信号的软件、独立 AI 网页和其他电脑的使用不能由这个本地观察器自动读取，详情会列明可观测范围。

悬停期间保持托盘图标稳定，详情继续读取每秒更新的数据；移开后图标恢复最新数值。`CodexQuotaTray.exe --check-hover <结果文件.json>` 可进行约 4 秒的置顶全屏覆盖、连续刷新稳定性和焦点保持检查，检查画面会自动关闭。

读取方案参考 [Tokscale](https://github.com/junhoyeo/tokscale)、[Tallyburn](https://github.com/seoulpro/tallyburn)、[Grok Build](https://github.com/xai-org/grok-build) 及 [Codex 官方遥测说明](https://learn.chatgpt.com/docs/config-file/config-advanced)。程序使用独立的 Python 数值读取器，不运行这些项目的守护进程。

## 构建与测试

```powershell
python -X utf8 -m unittest -v test_quota_sources.py test_tps_sources.py test_tray_app.py
.\build.ps1 -DistPath .\outputs
```

上述命令输出单文件、无控制台的 `outputs\CodexQuotaTray.exe`；不指定 `-DistPath` 时，维护脚本沿用本机部署约定，将 EXE 写入仓库的上一级目录。构建时的临时文件保存在 `work`，单文件程序的依赖解包到仓库上一级的 `runtime-temp`。若将 `CODEX_QUOTA_CODEX_EXE` 设置为 Codex 可执行文件路径，程序会优先使用该路径。

可用 `CodexQuotaTray.exe --check-grok <结果文件.json>` 单独检查打包后的 Grok 真实读取链路，成功退出码为 0；结果文件仅包含额度字段。程序在缺少标准句柄时初始化隐藏终端，并在查询结束后释放。打包显式包含 pywinpty 原生组件。替换 EXE 前应退出正在运行的旧版。

`CodexQuotaTray.exe --check-tps <结果文件.json>` 读取两次本机真实数据后退出，用于验证打包后的 TPS 读取器。
