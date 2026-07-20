#!/usr/bin/env python3
"""dws_event_bridge.py — dws event consume NDJSON → connect-log 格式转换（custom 层）

把 `dws event consume ... -f ndjson` 的事件流转成 event_watcher.py 的 log-tail 能解析的
"[connect] 收到 @user: text (convType=N convId=... msgId=...)" 行，写到 stdout（由
start_connect 重定向到 CONNECT_LOG）。

为什么需要它：
  - dws 事件的外层是 NDJSON，其中 data 字段是**再一层 JSON 字符串**，需二次解析
  - jq 未必可用，双层解析用 Python 最稳
  - event_watcher.REPLY_RE 期望特定文本格式，这里做适配（custom 特定，不进 core）

事件 data 内容路径（见 `dws event schema`）：
  data(JSON string) → .payload.body.{sender, content, openConversationId, openMessageId, createTime}

用法：
  dws event consume user_im_message_receive_group --group <cid> -f ndjson | python3 dws_event_bridge.py
"""

import json
import sys
import time

# convType 约定：1=单聊 o2o，2=群聊 group（event_watcher REPLY_RE 提取 convType=\d+）
_CONV_TYPE_BY_EVENT = {
    "user_im_message_receive_o2o": 1,
    "user_im_message_receive_at": 2,
    "user_im_message_receive_group": 2,
}


def _log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    # 诊断信息写 stderr（launchd/monitor 落盘），不污染 connect-log stdout
    print(f"[{ts}] [dws-bridge] {msg}", file=sys.stderr, flush=True)


def _to_connect_line(evt):
    """把一个 dws 事件对象转成 connect-log 行；无法解析返回 None。"""
    etype = evt.get("event_type") or evt.get("event_key") or ""
    raw = evt.get("data")
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        _log(f"data 二次解析失败 event_id={evt.get('event_id')}")
        return None

    body = (data.get("payload", {}) or {}).get("body", {}) or {}
    sender = body.get("sender", "未知发送人")
    content = (body.get("content", "") or "").replace("\n", " ").strip()
    conv_id = body.get("openConversationId", "")
    msg_id = body.get("openMessageId", "")
    conv_type = _CONV_TYPE_BY_EVENT.get(etype, 2)
    # @我(at) 事件天然是“被 @ 的消息”（payload 无显式 atUsers 字段，唯一可靠信号是事件类型）。
    # 打标 atMention=1 让 core.inbound.parse_line 解析进 extra['at_mention']，供 ack 回执判定
    # “群里被 @”这一路（#46）。放在 msgId 之后、) 之前——_CONVID_RE/_MSGID_RE 止于空白/)，不受影响。
    at_mention = etype == "user_im_message_receive_at"

    if not content:
        return None
    # 格式对齐 event_watcher.REPLY_RE：'\[connect\] 收到 @(.+?):\s*(.+?)\s+\(convType=(\d+)'
    tail = f"convType={conv_type} convId={conv_id} msgId={msg_id}"
    if at_mention:
        tail += " atMention=1"
    return f"[connect] 收到 @{sender}: {content} ({tail})"


def main():
    _log("bridge 启动，等待 dws event NDJSON …")
    seen = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            # 非事件行（如 dws 的状态 JSON），透传到 stderr 便于排查
            _log(f"跳过非 JSON 行: {line[:120]}")
            continue
        if evt.get("type") != "event":
            continue
        out = _to_connect_line(evt)
        if out:
            print(out, flush=True)  # → CONNECT_LOG
            seen += 1
    _log(f"stdin 结束，共处理 {seen} 条事件")


if __name__ == "__main__":
    main()
