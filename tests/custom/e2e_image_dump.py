#!/usr/bin/env python3
"""排查用：对一张真实图片端到端跑图片处理流程并打印每步真实内容。

流程: 读取图片字节 → 真实 vision 识别(_recognize_via_serve) → 打印真实 desc
      → 按 handle_image 的逻辑组装最终 prompt → 打印发给 opencode 的完整 prompt。

跳过了钉钉 download-media（需真实 mediaId/msgId），直接用本地图片文件。

用法:
    source config/constants.local.sh
    python3 tests/custom/e2e_image_dump.py [图片路径] [--user 张三] [--caption "这是什么"]

不传图片路径时用仓库 fixture (tests/custom/fixtures/math_1plus1.png)。
"""
import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from custom.capabilities import image as img  # noqa: E402
from custom import brain  # noqa: E402
from core.agent_common import find_serve_credentials  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "math_1plus1.png")
SEP = "=" * 72


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?", default=FIXTURE, help="图片路径")
    ap.add_argument("--user", default="张三")
    ap.add_argument("--caption", default="", help="随图说明文字")
    args = ap.parse_args()

    if not os.path.exists(args.image):
        sys.exit(f"图片不存在: {args.image}")

    # 前置检查
    if not img._VISION_MODEL:
        print("⚠️  AGENT_VISION_MODEL 未配置 —— serve 识别会跳过，回退外部 proxy。"
              "先 source config/constants.local.sh")
    _, port, _ = find_serve_credentials()
    if not port:
        print("⚠️  opencode serve 凭据缺失（serve 未运行？）识别可能失败。")

    mime = "image/jpeg" if args.image.lower().endswith((".jpg", ".jpeg")) else "image/png"
    with open(args.image, "rb") as f:
        img_bytes = f.read()

    print(SEP)
    print(f"[0] 输入图片: {args.image} ({len(img_bytes)}B, mime={mime})")
    print(f"    vision model = {img._VISION_MODEL!r}")
    print(SEP)
    print(f"[1] 发给 VISION 模型的 text prompt:\n{img._VISION_PROMPT}\n")

    # —— 真实识别 ——
    print(SEP)
    print("[2] 调用真实 vision 模型识别中 ...")
    desc = img._recognize_via_serve(img_bytes, mime=mime)
    if not desc:
        print("    serve 识别为空，回退外部 proxy ...")
        desc = img._proxy_vision(img_bytes)
    print(SEP)
    print(f"[2] vision 真实识别结果 desc (len={len(desc)}):\n{desc!r}\n")
    if not desc:
        sys.exit("识别结果为空，终止。")

    # —— 按 handle_image 组装最终 prompt ——
    parts = [
        f"用户 {args.user} 发送了一张图片。",
        "",
        "【图片识别内容】",
        "```",
        desc,
        "```",
        "",
    ]
    if args.caption:
        parts += [f"【用户随图说明】{args.caption}", ""]
    parts.append(img._IMAGE_PROMPT_FOOTER)
    user_prompt = "\n".join(parts)

    print(SEP)
    print(f"[3] 发给 OPENCODE 的 system 字段 (model={brain._OPENCODE_MODEL!r}):")
    print(SEP)
    print(brain._SYSTEM_PROMPT)
    print()
    print(SEP)
    print("[4] 发给 OPENCODE 的 user 部分 (raw=True, 无前缀) —— 这是最终 prompt:")
    print(SEP)
    print(user_prompt)


if __name__ == "__main__":
    main()
