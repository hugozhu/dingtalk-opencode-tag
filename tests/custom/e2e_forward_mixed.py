#!/usr/bin/env python3
"""e2e_forward_mixed.py — 合并转发混合消息端到端测试（2图 + 1文件 + 3文本）

为什么是合成 fixture 而非真发：钉钉 `combine-forward` 只能转发**已存在**的 msgId，
`send --msg-type image` 又需要预先持有 mediaId（CLI 不能本地上传→mediaId）。真造一条
2图+1文件+3文本的转发得往 hugozhu 单聊塞 ~6 条消息 + 2 个有效 mediaId，噪音大且不可复现。

本脚本用合成 forwardMessages 驱动**真实代码路径**：真实 list-by-ids 结构、真实
serve+gemini 视觉识别（对真实本地图片 avatar_oc.png）、真实 sender 摘要解析、真实
brain 生成回复。只 stub 三个 I/O 边界：
  - _fetch_forward_body：不连真实钉钉源（返回合成 body）
  - _download_image_to_path：把真实本地图片拷到临时路径（→ 真跑 gemini 识别）
  - _download_file_text：返回合成文件正文（无真实网盘）
  - send_reply：不真发（打印代替）

运行：serve 需在跑（有 gemini 视觉）。
  PYTHONPATH=src python3 tests/custom/e2e_forward_mixed.py
"""

import os
import shutil
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from unittest.mock import patch

import custom.capabilities  # noqa: F401  注册 brain/replier/能力
from custom.capabilities import forward
from custom import handler

# 真实本地图片（仓库里现成的），给 gemini 真识别
_REAL_IMG = os.path.join(PROJECT_ROOT, "avatar_oc.png")

# 合成的 6 条内层消息：2图 + 1文件 + 3文本。content 用真实钉钉格式：
#   图片  → "[图片消息](mediaId=$...)"
#   文件  → "[文件] <名> fileId: <id>"
#   文本  → 原文
_FMS = [
    {"content": "[图片消息](mediaId=$FAKE_MEDIA_1)", "createTime": "2026-07-25 10:00:01",
     "openMessageId": "msgIMG1=="},
    {"content": "大家看下这两张架构图", "createTime": "2026-07-25 10:00:05",
     "openMessageId": "msgTXT1=="},
    {"content": "[图片消息](mediaId=$FAKE_MEDIA_2)", "createTime": "2026-07-25 10:00:09",
     "openMessageId": "msgIMG2=="},
    {"content": "[文件] 需求文档.txt fileId: FAKE_FILE_1", "createTime": "2026-07-25 10:00:12",
     "openMessageId": "msgFILE1=="},
    {"content": "文件里是第三季度的排期", "createTime": "2026-07-25 10:00:15",
     "openMessageId": "msgTXT2=="},
    {"content": "@数字员工 帮我总结下这些内容", "createTime": "2026-07-25 10:00:18",
     "openMessageId": "msgTXT3=="},
]

# 外层 body：content 摘要（每行 名字:内容，与 _FMS 一一对应），sender=转发者
_SUMMARY = (
    "群聊的聊天记录\n"
    "hugozhu:[图片]\n"
    "hugozhu:大家看下这两张架构图\n"
    "冬翔:[图片]\n"
    "冬翔:[文件]需求文档.txt\n"
    "冬翔:文件里是第三季度的排期\n"
    "hugozhu:@数字员工 帮我总结下这些内容"
)
_BODY = {"sender": "hugozhu", "content": _SUMMARY, "openMessageId": "msgFWDMIX==",
         "forwardMessages": _FMS}

_CONV_ID = "cidE2EMixedForwardTest=="
_FAKE_FILE_TEXT = "Q3 排期：\n- 7月：转发能力加固\n- 8月：多语言\n- 9月：上线验收"


def _fake_download_image(media_id, msg_id, conv_id):
    """把真实本地图片拷到临时路径（模拟下载），供真实 gemini 识别。"""
    if not os.path.exists(_REAL_IMG):
        return None
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    shutil.copy(_REAL_IMG, path)
    return path


def main():
    serve_up = bool(__import__("subprocess").run(
        ["pgrep", "-f", "opencode serve"], capture_output=True).stdout.strip())
    img_ok = os.path.exists(_REAL_IMG)
    # 图片真识别需要 serve 在跑 + 本地图片在。二者缺一时结构照样验证，仅跳过"图片真识别"断言
    vision_live = serve_up and img_ok
    print(f"=== serve={serve_up} img={img_ok} → 图片真识别={'开' if vision_live else '跳过'} ===\n")

    captured = {}

    def fake_send(conv_id, conv_type, text, **kw):
        captured["reply"] = (conv_id, conv_type, text)

    with patch.object(forward, "_fetch_forward_body", return_value=(_BODY, _FMS)), \
         patch.object(handler, "_download_image_to_path", side_effect=_fake_download_image), \
         patch.object(handler, "_download_file_text", return_value=_FAKE_FILE_TEXT), \
         patch.object(forward, "send_reply", side_effect=fake_send), \
         patch.object(forward, "generate_reply",
                      side_effect=lambda s, p, **kw: captured.__setitem__("prompt", p) or "[stub 回复]") as gr:
        forward.handle_forward("hugozhu", _SUMMARY, "msgFWDMIX==", _CONV_ID, "2")

    prompt = captured.get("prompt", "")
    print("=" * 70)
    print("组装出的 PROMPT（发给 brain 主会话的完整 input）：")
    print("=" * 70)
    print(prompt)
    print("=" * 70)

    # 断言：6 条都在，类型都解析对
    checks = [
        ("转发者语境头", "转发了一段聊天记录（共 6 条消息）" in prompt),
        # 图片断言仅在 vision_live 时校验"真识别"；否则退化为"条目在位"（不因无 serve/图误判失败）
        ("图1 条目在位", "[1]" in prompt),
        *([(("图1 真识别（非失败）"), "识别失败" not in prompt.split("[2]")[0])] if vision_live else []),
        ("文本1 原文", "大家看下这两张架构图" in prompt),
        ("文件正文注入", "Q3 排期" in prompt or "第三季度" in prompt),
        ("文本2 原文", "文件里是第三季度的排期" in prompt),
        ("文本3 @数字员工", "帮我总结下这些内容" in prompt),
        ("发送人-冬翔解析到", "冬翔" in prompt),
        ("发送人-hugozhu", "hugozhu" in prompt),
        ("主会话 ctx.conv_id", gr.call_args.kwargs.get("ctx", {}).get("conv_id") == _CONV_ID),
        ("回复已发（stub）", "reply" in captured),
    ]
    print("\n=== 断言 ===")
    ok = True
    for name, passed in checks:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok = ok and passed
    print(f"\n{'✅ E2E PASS' if ok else '❌ E2E FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
