#!/usr/bin/env python3
"""e2e_vision_math_test.py — 视觉模型端到端联通测试（需真实 opencode serve）

不 mock：本地用 PIL 生成一张写着 "1+1=2" 的图片，经 image._recognize_via_serve
调用真实 AGENT_VISION_MODEL（默认 local/gemini-3.1-flash-image）读回文字，
断言识别结果里能看到这道算式。

前置：
  - opencode serve 在跑（.serve.port/.serve.pwd 存在，healthcheck 通过）
  - config/constants.local.sh 里 AGENT_VISION_MODEL 已配置

跑法：
  source config/constants.local.sh && python3 tests/custom/e2e_vision_math_test.py
serve 不可达或未配 vision 模型时 SKIP（而非 FAIL），保持 CI 友好。
"""

import io
import os
import re
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom.capabilities import image
from core.agent_common import find_serve_credentials


FIXTURE_PNG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "math_1plus1.png")


def _make_math_png(expr="1+1=2"):
    """优先读取仓库里的 fixture 图片；缺失时用 PIL 现场生成白底黑字算式 PNG，返回字节。"""
    if os.path.exists(FIXTURE_PNG):
        with open(FIXTURE_PNG, "rb") as f:
            return f.read()

    from PIL import Image, ImageDraw, ImageFont

    W, H = 480, 200
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ):
        try:
            font = ImageFont.truetype(path, 96)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    # 居中绘制
    bbox = draw.textbbox((0, 0), expr, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((W - tw) / 2 - bbox[0], (H - th) / 2 - bbox[1]), expr, fill="black", font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestVisionMathE2E(unittest.TestCase):
    def setUp(self):
        if not image._VISION_MODEL:
            self.skipTest("AGENT_VISION_MODEL 未配置 —— 先 source config/constants.local.sh")
        _, port, _ = find_serve_credentials()
        if not port:
            self.skipTest("opencode serve 凭据缺失（serve 未运行？）")

    def test_reads_back_one_plus_one(self):
        expr = "1+1=2"
        img_bytes = _make_math_png(expr)
        print(f"\n[e2e] 生成图片 {len(img_bytes)}B，算式={expr!r}，模型={image._VISION_MODEL}")

        desc = image._recognize_via_serve(img_bytes, mime="image/png")
        print(f"[e2e] 视觉模型识别结果：\n{desc}\n")

        self.assertTrue(desc, "视觉模型返回空 —— 识别失败或 serve 不可达")
        # 去掉所有空白后匹配 1+1（模型可能写成 "1 + 1" 或 "1+1=2"）
        compact = re.sub(r"\s+", "", desc)
        self.assertIn("1+1", compact, f"识别结果里没看到 1+1：{desc!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
