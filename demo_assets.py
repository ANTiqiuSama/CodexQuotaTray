"""Render README demo images from demo data: python demo_assets.py"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

import agent_panel as ap
from dataclasses import replace


def demo_models() -> tuple[ap.PanelModel, ...]:
    bubble = ap.BubbleModel
    busy = ap.PanelModel(
        "codex", "C", "GPT / Codex", "2 个在工作",
        (("5 小时", 72.0, "2 小时 13 分后重置"), ("每周", 38.0, "3 天 4 小时后重置")), 85.0,
        (bubble("a", "重构悬停面板", "编辑 hover_details.py", "C", True, "重构悬停面板气泡交互"),
         bubble("b", "补齐测试", "运行测试", "C", True),
         bubble("d", "README 文档", "刚完成", "G", False)),
        "live", "GPT 社区重置预测 · 24h 22% · 48h 39%",
        "5 小时额度：按当前速度，预计 2 小时 40 分后用完，早于下次重置约 35 分钟", "warn")
    idle = ap.PanelModel("grok", "X", "Grok", "空闲", (("2 小时", 100.0, "已到重置时间"),), None, (), "idle")
    mimo = ap.PanelModel("mimo", "M", "Xiaomi MiMo", "空闲", (("套餐额度", 96.9, ""),), 0, (), "idle",
                         forecast="按近期速度可用到到期，预计剩余 82%", forecast_tone="ok", today_used=1.53e9)
    upcoming = (ap.ResetColumn("gemini", "G", "Gemini · Opus", "Claude/GPT · 5 小时", "10-20 23:10", "1 小时 12 分后"),
                ap.ResetColumn("codex", "C", "Codex", "5 小时", "10-21 01:20", "3 小时 22 分后"),
                ap.ResetColumn("gemini", "G", "Gemini · Argon", "Argon · 每周", "10-26 11:02", "5 天 13 小时后"))
    return tuple(replace(model, upcoming_resets=upcoming) for model in (busy, idle, mimo))


def render(destination: Path, scale: float = 1.5) -> Path:
    frames = []
    for model in demo_models():
        scene, moment = ap.Scene(), 0.0
        scene.set_model(model, moment)
        for _ in range(120):
            moment += 1 / 60
            scene.step(moment, None, False, False)
        frames.append(scene.render(scale))
    sheet = Image.new("RGBA", (sum(f.width for f in frames), max(f.height for f in frames)), (31, 52, 74, 255))
    x = 0
    for frame in frames:
        sheet.alpha_composite(frame, (x, 0))
        x += frame.width
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(destination, optimize=True)
    return destination


if __name__ == "__main__":
    print(render(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("assets") / "panel-demo.png"))
