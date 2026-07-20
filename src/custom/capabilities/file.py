"""file — 文档/文件消息处理能力（custom 插件）(#40)

群里发文件时，event-consume 下 content 形如
`[文件] <文件名> fileId: <fileId> 注意：如需下载使用dws drive download命令下载`。

**受控处理**（对齐 image 能力）：harness **主动**把文件下载到**临时目录**（不是项目
工作目录）、读前 N 字节文本、注入 agent、回复、用完删。这样避免了"agent 自主用 bash
工具下载文件到项目目录/执行 shell"的不可控 + 安全问题（见 #40）。

流程：
  1. on_inbound(kind=file)：防回环 → 去重 → 提交 handle_file。
  2. handle_file：提取 fileId + 文件名 → drive download 到 tmpdir → 读前 N 字节 →
     文本类文件组 prompt（文件名 + 正文）注入 brain → 回复发回群 → 删 tmpdir。
  3. 下载失败 / 二进制无法读 → 明确告知用户，不静默、不硬塞乱码。

开关：CAP_FILE_ENABLED（默认开）。优先级 40（与 image 同级，先于 forward/text）。
"""

import os
import re
import shutil
import tempfile

from core.agent_common import _run_cli, log, submit_handler
from core.capabilities import Capability, register
from core.inbound import KIND_FILE
from custom.brain import generate_reply
from custom.replier import send_reply

# 从 content 提取 fileId 和文件名
# 格式：[文件] <文件名> fileId: <fileId> 注意：...
_RE_FILE_ID = re.compile(r"fileId:\s*(\S+)")
_RE_FILE_NAME = re.compile(r"\[文件\]\s*(.+?)\s+fileId:")

# 读取正文的字节上限（防超大文件撑爆 prompt）
_FILE_MAX_BYTES = int(os.environ.get("CAP_FILE_MAX_BYTES", "16384"))

# 文本类文件后缀（这些直接读正文；其它当二进制，不硬读）
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log",
    ".yaml", ".yml", ".xml", ".ini", ".conf", ".cfg", ".toml", ".py", ".js",
    ".ts", ".sh", ".go", ".java", ".c", ".cpp", ".h", ".rs", ".rb", ".php",
    ".sql", ".html", ".css", ".env", ".properties", ".gitignore",
}

# 防回环 + msgId 去重由 core 声明式处理（见 Capability(loop_guard/dedup)）。

# prompt 末句
_FILE_PROMPT_FOOTER = os.environ.get(
    "CAP_FILE_PROMPT_FOOTER",
    "以上是用户发送的文件内容（由系统下载并读取，可能已截断）。请结合用户随文件的说明（若有），"
    "对用户的意图做出有帮助的回应（该答疑答疑、该归纳归纳）。",
)


def _is_text_file(filename):
    _, ext = os.path.splitext((filename or "").lower())
    return ext in _TEXT_EXTS


def _download_file(file_id):
    """drive download 到临时目录（**非项目目录**），返回本地路径或 None。"""
    tmp_dir = tempfile.mkdtemp(prefix="agent_file_")
    rc, _ = _run_cli([
        "drive", "download",
        "--node", file_id,
        "--output", tmp_dir + "/",
    ], timeout=60)
    if rc != 0:
        log(f"file: 下载失败 rc={rc} fileId={file_id[:16]}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    for name in os.listdir(tmp_dir):
        return os.path.join(tmp_dir, name)
    log(f"file: 下载目录为空 fileId={file_id[:16]}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def _read_text(path):
    """读文件前 N 字节文本，返回 (text, truncated) 或 (None, False) 读失败。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(_FILE_MAX_BYTES + 1)
        truncated = len(data) > _FILE_MAX_BYTES
        return data[:_FILE_MAX_BYTES], truncated
    except Exception as e:
        log(f"file: 读取失败 {e}")
        return None, False


def handle_file(user, text, msg_id, conv_id, conv_type):
    """提取 fileId+文件名 → 受控下载到 tmpdir → 读正文 → 注入 brain → 回复 → 删 tmpdir。"""
    fid_m = _RE_FILE_ID.search(text or "")
    if not fid_m:
        log(f"file: 未提取到 fileId msgId={msg_id[:24]}")
        return
    file_id = fid_m.group(1)
    name_m = _RE_FILE_NAME.search(text or "")
    filename = name_m.group(1).strip() if name_m else "（未知文件名）"

    if not _is_text_file(filename):
        # 二进制/未知类型：不硬读乱码，明确告知
        send_reply(conv_id, conv_type,
                   f"收到文件「{filename}」，但它看起来不是文本类文件，我暂时读不了内容。"
                   f"文本文件（txt/md/csv/json/日志/代码等）我可以直接读。")
        return

    path = _download_file(file_id)
    if not path:
        send_reply(conv_id, conv_type, f"抱歉，文件「{filename}」我没能下载下来，能再发一次吗？")
        return

    tmp_dir = os.path.dirname(path)
    try:
        content, truncated = _read_text(path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)  # 用完即删，不留临时文件

    if content is None:
        send_reply(conv_id, conv_type, f"抱歉，文件「{filename}」的内容我读取失败了。")
        return

    log(f"file: msgId={msg_id[:24]} filename={filename!r} content_len={len(content)} truncated={truncated}")

    parts = [f"用户 {user} 发送了一个文件：{filename}", "", "【文件内容】", content]
    if truncated:
        parts.append("…（文件内容过长，已截断）")
    parts += ["", _FILE_PROMPT_FOOTER]
    prompt = "\n".join(parts)

    reply = generate_reply(user, prompt, raw=True)
    if reply:
        send_reply(conv_id, conv_type, reply)
    else:
        log(f"file: 大脑无回复 msgId={msg_id[:24]}")


def on_inbound(msg):
    """文件消息入站：提交 handle_file。返回 True=已消费。

    防回环 + msgId 去重由 core dispatch_inbound（loop_guard/dedup 声明）处理。
    """
    submit_handler(handle_file, msg.user, msg.text, msg.msg_id, msg.conv_id, msg.conv_type)
    return True


CAPABILITY = Capability(
    name="file",
    on_inbound=on_inbound,
    handles_kinds={KIND_FILE},
    priority=40,             # 与 image 同级，先于 forward(50)/text(100)
    default_enabled=True,
    loop_guard=True,         # core 统一防回环
    dedup=True,              # core 统一 msgId 去重
)
register(CAPABILITY)
