"""handler.py — 业务 handler（FDE 在这里实现业务逻辑）

本文件是 FDE 的主要定制点。初始内容复制自 src/templates/handler_template.py，
FDE 按自己业务调整：
  1. 常量正则（BUSINESS_MSG_RE / MSGID_RE / _RE_MEDIA_ID / _RE_FILE_ID）
  2. _classify_message 消息分类逻辑
  3. fetch_attachments + _fetch_image_entry + _fetch_file_entry 附件下载
  4. render_prompt 末句 prompt
  5. _predicate 匹配自己业务消息特征
  6. make_reply_msgs 通知消息格式

业务路由注册在 custom/routes.py（不要改 core/event_watcher.py）。

upstream 升级 handler_template.py 后，FDE 可 diff 参考新最佳实践：
  diff src/templates/handler_template.py src/custom/handler.py
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
from collections import OrderedDict

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from core.agent_common import (
    _abort_and_clean_session,
    _run_cli,
    _find_bot_session,
    _find_session_with_predicate,
    _md,
    _proxy_vision,
    inject_and_forward,
    log,
    send_notification,
    submit_handler,
)

# ---------------------------------------------------------------------------
# Constants & regex — 用户按自己的消息格式调整
# ---------------------------------------------------------------------------

# 业务消息正文里附件最大内联字节数
ATTACHMENT_MAX_BYTES = 16384

# 轮询等待依赖服务转发完成的参数（测试 patch 为 0）
_POLL_MAX_SECONDS = 60
_POLL_INTERVAL = 5

# 通用：检测消息类型的正则（用户按业务调整）
_RE_MEDIA_ID = re.compile(r"mediaId=([^\s)]+)")
_RE_FILE_ID = re.compile(r"fileId:\s*(\S+)")

# 业务消息检测正则（log-tail 用来识别）
BUSINESS_MSG_RE = re.compile(r'msgtype="business-special"')
MSGID_RE = re.compile(r'msgId=([^\s)]+)')

# 已处理的 msgId 去重 + 跨行状态
# 有界去重：长驻进程里 msgId 只增不删会内存泄漏，用 OrderedDict 当 FIFO 上限
_SEEN_MAX = 4096


class _BoundedSeen:
    """FIFO 上限的去重集合（超出 maxlen 时淘汰最旧的）。"""
    def __init__(self, maxlen):
        self._d = OrderedDict()
        self._maxlen = maxlen

    def __contains__(self, k):
        return k in self._d

    def add(self, k):
        self._d[k] = None
        if len(self._d) > self._maxlen:
            self._d.popitem(last=False)

    def clear(self):
        self._d.clear()


_seen = _BoundedSeen(_SEEN_MAX)
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
    # 拷贝，避免就地改调用方传入的 list（保持纯函数语义）
    senders = list(senders)
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


# ---------------------------------------------------------------------------
# Orchestration — list-by-ids → fetch → render → cleanup → inject_and_forward
# ---------------------------------------------------------------------------

def handle_message(msg_id, original_convs=None):
    """业务消息处理：反查消息体 → fetch + render → cleanup spurious 轮次 → inject_and_forward。

    Args:
        msg_id: 业务消息的 openMessageId
        original_convs: 从日志提取的原始会话 ID 列表，用于反查附件的 fileId
    """
    import time as _time
    # asked_ts 用于过滤需要 DELETE 的"多余轮次"消息。-5s buffer 容忍时钟偏移
    asked_ts_ms = int(_time.time() * 1000) - 5000
    log(f"handle: msgId={msg_id} asked_ts={asked_ts_ms}")

    # 1. 反查完整消息体
    rc, out = _run_cli([
        "chat", "message", "list-by-ids",
        "--msg-ids", msg_id,
    ], timeout=30)
    if rc != 0:
        log(f"list-by-ids failed rc={rc}")
        send_notification("⚠️ 处理失败",
                          _md("处理失败", f"⚠️ 反查消息体失败 (rc={rc})", f"msgId: `{msg_id}`"))
        return
    try:
        d = json.loads(out)
        msgs = d.get("result", {}).get("messages", [])
    except Exception as e:
        log(f"parse list-by-ids response err: {e}")
        return
    if not msgs:
        log(f"no message found for msgId={msg_id}")
        return

    body = msgs[0]
    content = body.get("content", "") or ""
    sender = body.get("sender", "用户")
    messages = body.get("messages") or body.get("forwardMessages") or []
    if not messages:
        log(f"no messages in msgId={msg_id}")
        return

    send_notification("📨 处理中", _md(
        "处理中",
        f"🔍 检测到消息（{len(messages)} 条），正在解析…",
        f"msgId: `{msg_id}`"
    ))

    # 2. fetch attachments (I/O) + render prompt (pure)
    raw_senders = []  # 用户按业务调整：从 summary 文本解析 senders
    # 诊断：summary 行数与 messages 数量不一致时记 raw content 头 300 字符
    if len(raw_senders) != len(messages):
        preview = content[:300].replace("\n", " | ")
        log(f"senders mismatch msgId={msg_id} "
            f"senders={len(raw_senders)} msgs={len(messages)} content[:300]={preview!r}")

    senders = _fetch_senders(messages, raw_senders)
    attachments = fetch_attachments(messages, lookup_convs=original_convs)
    prompt = render_prompt(body, senders, attachments, sender)
    if not prompt:
        log(f"render_prompt returned None for msgId={msg_id}")
        return

    # 3. Cleanup 依赖服务转发的原始 JSON 轮次
    #    依赖服务可能延迟转发，**轮询**等待命中（每 POLL_INTERVAL 秒一次，
    #    最多 POLL_MAX_SECONDS 秒），命中后立即 abort+cleanup，阻止 LLM 处理原始 JSON
    import time as _time_poll
    # 用户实现 _predicate 匹配自己业务消息的特征（如含 'msgtype=business-special'）
    def _predicate(msg):
        text = "".join(p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text")
        return 'msgtype="business-special"' in text

    fwd_sid = _find_session_with_predicate(_predicate, asked_ts_ms=asked_ts_ms)
    poll_deadline = _time_poll.time() + _POLL_MAX_SECONDS
    while not fwd_sid and _time_poll.time() < poll_deadline:
        _time_poll.sleep(_POLL_INTERVAL)
        fwd_sid = _find_session_with_predicate(_predicate, asked_ts_ms=asked_ts_ms)
    if fwd_sid:
        aborted, deleted = _abort_and_clean_session(fwd_sid, asked_ts_ms)
        log(f"cleanup session={fwd_sid[:12]}... aborted={aborted} deleted={deleted}")
    else:
        log(f"no business session found after {_POLL_MAX_SECONDS}s polling")

    # 4. inject_and_forward: 公共模板负责 find/create 会话 → post → get reply → send_notification
    msg_count = len(messages)
    prompt_preview = prompt[:3500]
    inject_and_forward(
        prompt=prompt,
        session_title="agent-handler",
        make_reply_msgs=lambda reply: [
            ("📨 解析结果", _md("解析结果", "📋 从消息提取的内容：", prompt_preview)),
            (f"📨 总结（{msg_count} 条）", reply),  # reply 直接作正文，不被 _md 的 ** 包裹
        ],
        make_no_session_msg=lambda: (
            "⚠️ 无法处理",
            _md("处理失败", "⚠️ 无法找到或创建 agent 会话", "agent serve 可能未运行，请稍后重试。")
        ),
        make_no_reply_msg=lambda: (
            "⚠️ 无回复",
            _md("处理失败", "⚠️ agent 未生成回复", "")
        ),
    )


def handle_message_async(msg_id, original_convs=None):
    """Submit handle_message to the bounded handler pool (matches log_tail usage)."""
    submit_handler(handle_message, msg_id, original_convs)
