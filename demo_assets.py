"""Render README demo images from demo data: python demo_assets.py"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

import agent_panel as ap


def demo_models() -> tuple[ap.PanelModel, ap.PanelModel]:
    bubble = ap.BubbleModel
    busy = ap.PanelModel(
        "codex", "C", "GPT / Codex", "2 个在工作",
        (("5 小时", 72.0, "2 小时 13 分后重置"), ("每周", 38.0, "3 天 4 小时后重置")), 85.0,
        (bubble("a", "重构悬停面板", "编辑 hover_details.py", "C", True, "重构悬停面板气泡交互"),
         bubble("b", "补齐测试", "运行测试", "C", True),
         bubble("d", "README 文档", "刚完成", "M", False)),
        "live", "24 小时内全局重置概率 72%",
        (ap.ScheduleChip("s1", "依赖巡检", True, "C"), ap.ScheduleChip("s2", "每日日报汇总", False, "", "上次 09:00"),
         ap.ScheduleChip("s3", "清理", False, "", "待运行")))
    idle = ap.PanelModel("grok", "G", "Grok", "空闲", (("2 小时", 100.0, "已到重置时间"),), None, (), "idle")
    return busy, idle


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
