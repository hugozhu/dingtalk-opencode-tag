#!/usr/bin/env python3
"""排查用：打印图片处理流程中真正发给两个模型的完整提示词。

用法:
    source config/constants.local.sh
    python3 tests/custom/dump_image_prompts.py

会加载真实模块常量（含环境变量/local 覆盖），并用示例 desc/caption 组装
出与线上一致的 vision prompt + opencode(system+user) prompt。
把下面的 SAMPLE_* 换成你要排查的真实值即可。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from custom.capabilities import image as img  # noqa: E402
from custom import brain  # noqa: E402

# —— 排查时替换成真实值 ——
SAMPLE_USER = "张三"
SAMPLE_DESC = "<这里是 vision 模型识别出来的文本>"
SAMPLE_CAPTION = "这张图什么意思"   # 无随图说明时设为 ""

SEP = "=" * 70


def dump_vision_prompt():
    print(SEP)
    print(f"[1] 发给 VISION 模型的 text part  (model={img._VISION_MODEL!r})")
    print(SEP)
    print(img._VISION_PROMPT)
    print()


def dump_opencode_prompt():
    parts = [
        f"用户 {SAMPLE_USER} 发送了一张图片。",
        "",
        "【图片识别内容】",
        "```",
        SAMPLE_DESC,
        "```",
        "",
    ]
    if SAMPLE_CAPTION:
        parts += [f"【用户随图说明】{SAMPLE_CAPTION}", ""]
    parts.append(img._IMAGE_PROMPT_FOOTER)
    user_prompt = "\n".join(parts)

    print(SEP)
    print(f"[2] 发给 OPENCODE 模型的 system 字段  (model={brain._OPENCODE_MODEL!r})")
    print(SEP)
    print(brain._SYSTEM_PROMPT)
    print()
    print(SEP)
    print("[3] 发给 OPENCODE 模型的 user 部分 (raw=True, 无 '{user}：' 前缀)")
    print(SEP)
    print(user_prompt)
    print()


if __name__ == "__main__":
    dump_vision_prompt()
    dump_opencode_prompt()
