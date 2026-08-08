"""handler_template.py — 业务 handler 模板

提炼自: dingtalk-opencode-agent/forward_handler.py
原作者: hugozhu

示范 4 个最佳实践：

1. **渲染/IO 分层**：fetch_attachments（I/O 集中）vs render_prompt（纯函数零 I/O）
   分开测试 + 便于后续并行化
2. **批量反查**：多 msgId 一次 list-by-ids 批量反查 sender，比逐个 list --group
   快且不依赖群权限
3. **诊断日志**：数量不匹配时记 raw 输入头 N 字符，便于排查外部 API 格式变化
4. **只管解析，不管生成**：本模板负责「消息 → prompt」，拿到 prompt 之后怎么生成
   回复交给能力调 core.brain.generate_reply(ctx={"conv_id": ...})，会话复用按 conv
   由 custom/brain.py 统一管（AGENT_SESSION_REUSE）——handler 不自己找/建 session。

这是一个**通用 handler 模板**——业务逻辑（消息分类、附件下载、prompt 拼接）需要
用户按自己场景实现。本文件示范结构 + 关键 API 调用模式。
"""

import json
import os
import re
import sys
import tempfile
import threading
import time

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core.agent_common import (
    _run_cli,
    _proxy_vision,
    log,
)

# ---------------------------------------------------------------------------
# Constants & regex — 用户按自己的消息格式调整
# ---------------------------------------------------------------------------

# 业务消息正文里附件最大内联字节数
ATTACHMENT_MAX_BYTES = 16384

# 通用：检测消息类型的正则（用户按业务调整）
_RE_MEDIA_ID = re.compile(r"mediaId=([^\s)]+)")
_RE_FILE_ID = re.compile(r"fileId:\s*(\S+)")

# 业务消息检测正则（log-tail 用来识别）
BUSINESS_MSG_RE = re.compile(r'msgtype="business-special"')
MSGID_RE = re.compile(r'msgId=([^\s)]+)')

# 已处理的 msgId 去重 + 跨行状态
_seen = set()
_pending_cross_line = False
_pending_cross_convs = []
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Detection — log-tail 调用，封装跨行检测 + 去重
# ---------------------------------------------------------------------------

def match_business_line(line):
    """Check if a log line matches business message format and extract msgId + convs.

    Handles two formats:
      - Single-line: `... msgtype="business-special" ... msgId=msgXXX ...`
      - Cross-line:  line 1 has msgtype, line 2 has msgId

    Returns (msgid, convs) tuple when matched and msgId is new, None otherwise.
    Side effect: mutates module-level _pending_cross_line / _pending_cross_convs
    for cross-line state. Dedup via _seen.
    """
    global _pending_cross_line, _pending_cross_convs

    with _state_lock:
        if _pending_cross_line:
            _pending_cross_line = False
            mid_m = MSGID_RE.search(line)
            if mid_m:
                mid = mid_m.group(1)
                if mid in _seen:
                    return None
                _seen.add(mid)
                return mid, []
            return None

        if not BUSINESS_MSG_RE.search(line):
            return None
        mid_m = MSGID_RE.search(line)
        if mid_m:
            mid = mid_m.group(1)
            if mid in _seen:
                return None
            _seen.add(mid)
            return mid, []
        _pending_cross_line = True
        return None


def reset_dedup_state():
    """Clear dedup state (tests only)."""
    global _pending_cross_line, _pending_cross_convs
    with _state_lock:
        _seen.clear()
        _pending_cross_line = False
        _pending_cross_convs = []


# ---------------------------------------------------------------------------
# Pure parsing helpers — 用户实现自己的分类逻辑
# ---------------------------------------------------------------------------

def _classify_message(content):
    """Classify a message by content. Returns 'image' / 'file' / 'text'.

    用户按业务调整：图片有 mediaId、文件有 fileId、其他是 text。
    """
    if "[图片消息]" in content or "mediaId=" in content:
        return "image"
    if "[文件]" in content or "fileId:" in content:
        return "file"
    return "text"


# ---------------------------------------------------------------------------
# IO helpers — downloads / vision（用户实现自己的下载逻辑）
# ---------------------------------------------------------------------------

def _download_image_to_path(media_id, msg_id, conv_id):
    """Download an image via CLI to a temp file. Returns local path or None."""
    tmp_dir = tempfile.mkdtemp(prefix="agent_img_")
    rc, _ = _run_cli([
        "chat", "message", "download-media",
        "--type", "mediaId",
        "--resource-id", media_id,
        "--message-id", msg_id,
        "--open-conversation-id", conv_id,
        "--output", tmp_dir + "/",
    ])
    if rc != 0:
        log(f"image download failed (rc={rc}) mediaId={media_id[:24]}")
        return None
    for name in os.listdir(tmp_dir):
        return os.path.join(tmp_dir, name)
    return None


def _download_file_text(file_id):
    """Download a file via CLI and return its text content (first N bytes)."""
    tmp_dir = tempfile.mkdtemp(prefix="agent_file_")
    rc, _ = _run_cli([
        "drive", "download",
        "--node", file_id,
        "--output", tmp_dir + "/",
    ])
    if rc != 0:
        log(f"file download failed (rc={rc}) fileId={file_id}")
        return "[文件下载失败]"
    for name in os.listdir(tmp_dir):
        path = os.path.join(tmp_dir, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(ATTACHMENT_MAX_BYTES)
        except Exception as e:
            log(f"read downloaded file failed: {e}")
            return "[文件内容读取失败]"
    return "[文件为空]"


# ---------------------------------------------------------------------------
# Fetch stage — resolve attachments → list of dicts (pure data, no rendering)
# ---------------------------------------------------------------------------

def fetch_attachments(messages, lookup_convs=None):
    """Resolve each message into an attachment dict with raw content + fetched data.

    I/O stage: all downloads + vision calls happen here. Returns pure data list.

    Returns list of dicts: [{type, raw_content, text, time, msgid, conv_id}, ...]
    """
    out = []
    for fm in messages:
        fm_content = fm.get("content", "") or ""
        fm_msg_id = fm.get("openMessageId", "")
        fm_conv_id = fm.get("openConversationId", "")
        fm_time = fm.get("createTime", "")
        kind = _classify_message(fm_content)

        if kind == "image":
            entry = _fetch_image_entry(fm_content, fm_msg_id, fm_conv_id)
        elif kind == "file":
            entry = _fetch_file_entry(fm_content)
        else:
            entry = fm_content

        out.append({
            "type": kind,
            "raw_content": fm_content,
            "text": entry,
            "time": fm_time,
            "msgid": fm_msg_id,
            "conv_id": fm_conv_id,
        })
    return out


def _fetch_image_entry(fm_content, msg_id, conv_id):
    """Resolve an image message to its prompt entry text."""
    mid_m = _RE_MEDIA_ID.search(fm_content)
    if not mid_m:
        return "[图片消息，未提取到 mediaId]"
    media_id = mid_m.group(1)
    image_path = _download_image_to_path(media_id, msg_id, conv_id)
    if not image_path:
        return "[图片，下载失败]"
    try:
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        desc = _proxy_vision(img_bytes)
    except Exception as e:
        log(f"image recognize err: {e}")
        desc = ""
    try:
        os.unlink(image_path)
    except Exception:
        pass
    if desc:
        return f"[图片，识别内容]\n```\n{desc}\n```"
    return "[图片，识别失败]"


def _fetch_file_entry(fm_content):
    """Resolve a file message to its prompt entry text."""
    fid_m = _RE_FILE_ID.search(fm_content)
    if not fid_m:
        return f"{fm_content}\n    [文件正文下载失败：未获取到 fileId]"
    file_id = fid_m.group(1)
    file_text = _download_file_text(file_id)
    if len(file_text) > ATTACHMENT_MAX_BYTES:
        file_text = file_text[:ATTACHMENT_MAX_BYTES] + "\n...(文件内容过长，已截断)"
    return f"{fm_content}\n    文件正文：\n```\n{file_text}\n```"


# ---------------------------------------------------------------------------
# Batch reverse lookup — 一次 list-by-ids 批量反查多条 sender
# ---------------------------------------------------------------------------

def _lookup_senders_batch(msg_ids):
    """Batch reverse lookup: one list-by-ids call returns multiple senders.

    比 list --group 鲁棒（不依赖群权限）且更快（一次调用批量取回）。
    """
    if not msg_ids:
        return {}
    rc, out = _run_cli([
        "chat", "message", "list-by-ids",
        "--msg-ids", ",".join(msg_ids),
    ], timeout=30)
    if rc != 0:
        log(f"list-by-ids 批量反查 sender 失败 rc={rc}")
        return {}
    try:
        d = json.loads(out)
        msgs = d.get("result", {}).get("messages", [])
    except Exception as e:
        log(f"解析 list-by-ids 响应失败: {e}")
        return {}
    result = {}
    for m in msgs:
        mid = m.get("openMessageId", "")
        s = m.get("sender")
        if mid and s and s != "null":  # 过滤 DingTalk API quirk
            result[mid] = s
    return result


def _fetch_senders(messages, fallback_senders):
    """补齐 sender 列表到 len(messages) via batch reverse lookup."""
    senders = list(fallback_senders)[:len(messages)]
    while len(senders) < len(messages):
        senders.append(None)
    senders = [None if s == "未知发送人" else s for s in senders]

    missing_indices = [i for i, s in enumerate(senders) if s is None]
    if missing_indices:
        missing_msg_ids = [messages[i].get("openMessageId", "") for i in missing_indices]
        missing_msg_ids = [mid for mid in missing_msg_ids if mid]
        log(f"{len(missing_indices)} 条消息缺 sender，批量反查 list-by-ids")
        sender_map = _lookup_senders_batch(missing_msg_ids)
        for i in missing_indices:
            mid = messages[i].get("openMessageId", "")
            s = sender_map.get(mid)
            if s:
                senders[i] = s
                log(f"反查到 sender msgId={mid[:30]} sender={s!r}")
            else:
                senders[i] = "未知发送人"
                log(f"反查 sender 失败 msgId={mid[:30]}")
    return senders


# ---------------------------------------------------------------------------
# Render stage — pure function, zero I/O
# ---------------------------------------------------------------------------

def render_prompt(body, senders, attachments, sender):
    """Render the structured prompt from body + already-fetched attachments.

    Pure function — no I/O, no subprocess, no network. Easy to unit test.
    Returns the assembled prompt string, or None when no messages.
    """
    messages = body.get("messages") or []
    if not messages:
        return None
    while len(senders) < len(messages):
        senders.append("未知发送人")

    lines = [f"用户 {sender} 转发了一段消息（共 {len(messages)} 条）：\n"]
    for i, (fm, att) in enumerate(zip(messages, attachments)):
        fm_time = att.get("time") or fm.get("createTime", "")
        fm_sender = senders[i] if i < len(senders) else "未知发送人"
        entry = att.get("text", "") or fm.get("content", "")
        lines.append(f"[{i + 1}] [{fm_time}] {fm_sender}: {entry}\n")
    # 用户按业务调整末句 prompt
    lines.append("请基于上述消息内容回应用户。")
    return "\n".join(lines)


