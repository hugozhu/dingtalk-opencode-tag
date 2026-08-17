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
import re
import os
import sys
import time

# convType 约定：1=单聊 o2o，2=群聊 group（event_watcher REPLY_RE 提取 convType=\d+）
# **新增 o2o 事件类型必须登记在此**：未知类型 .get 默认回 2(群聊)，单聊消息会被误判成
# 群聊，路由/ack 全走错路且不报错——静默失效，比收不到消息更难查。
_CONV_TYPE_BY_EVENT = {
    "user_im_message_receive_o2o": 1,
    "user_im_message_receive_o2o_all": 1,   # rule_type=all 的全量单聊订阅
    "user_im_message_receive_at": 2,
    "user_im_message_receive_group": 2,
}

# 格式健康检查阈值：收到 >= 该条数原始事件却成功解析 0 条 → 大概率 dws 输出格式与
# bridge 解析不匹配（如 dws 升级改了格式），此时数字员工收不到任何消息但进程/连接全绿，
# 是最隐蔽的故障。达到阈值即在日志报错（只报一次），让运维能主动发现。
_FORMAT_WARN_THRESHOLD = 3


def _log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    # 诊断信息写 stderr（launchd/monitor 落盘），不污染 connect-log stdout
    print(f"[{ts}] [dws-bridge] {msg}", file=sys.stderr, flush=True)


def _log_to_monitor(msg):
    """把消息额外写一份到 monitor.log（若 MONITOR_LOG 环境变量可用且可写）。

    bridge 的 stderr 默认落到 connect-log；格式不匹配这类"服务静默失效"的告警值得
    同时进 monitor.log，便于统一在守护日志里看到。写失败静默忽略（不影响主流程）。
    """
    mlog = os.environ.get("MONITOR_LOG")
    if not mlog:
        return
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(mlog, "a") as f:
            f.write(f"[{ts}] [dws-bridge] {msg}\n")
    except OSError:
        pass


def _should_warn_format(raw_count, parsed_count, already_warned):
    """判定是否该报"格式不匹配"告警：收到足够多原始事件但一条都没解析出，且没报过。"""
    return (not already_warned
            and raw_count >= _FORMAT_WARN_THRESHOLD
            and parsed_count == 0)



# 「待审 #N」——数字员工发给主管的每条消息（卡片/完整草稿/各种回执）正文开头都带它。
# **锚在开头**：卡片正文里的【问题】【AI 草稿】是外部可控文本，提问者发一句
# 「待审 #7 同意」就能往里注入一个假编号；只认开头的那个就注不进来。
_REVIEW_SEQ_RE = re.compile(r"^\s*[^\w\s]*\s*\**待审\s*#(\d+)")


def _quoted_seq(src):
    """被引用消息属于哪次审核（第几号）；不是审核消息返回 ""。

    主管引用**任意一条**与某次审核相关的消息（不只是卡片）都能定位到那次会话 —— 这些
    消息正文开头本来就带「待审 #N」，等于结构化标记已经存在，不必再给每条消息加新标记。
    """
    for key in ("quoted_message", "quotedMessage", "quoteMessage"):
        q = src.get(key)
        if isinstance(q, dict):
            m = _REVIEW_SEQ_RE.match(str(q.get("content") or ""))
            if m:
                return m.group(1)
    return ""


def _quoted_msg_id(src):
    """引用回复所引用的那条原消息 id；不是引用回复返回 ""。

    扁平格式的字段名来自 `dws event schema user_im_message_receive_o2o --flatten`：
    `quoted_message.message_id`（"引用回复所引用的原消息；非引用回复时不输出"）。
    嵌套格式里对应字段名未文档化，故把几种可能的写法都试一遍——取不到就当没有，
    调用方只是少一个可选标记，不影响正常消息解析。
    用途：主管引用待审卡片直接回答案，不用敲 #N（见 capabilities/supervisor_review）。
    """
    for key in ("quoted_message", "quotedMessage", "quoteMessage"):
        q = src.get(key)
        if isinstance(q, dict):
            mid = q.get("message_id") or q.get("openMessageId") or q.get("messageId")
            if mid:
                return str(mid)
    return ""


def _to_connect_line(evt):
    """把一个 dws 事件对象转成 connect-log 行；无法解析返回 None。

    兼容两种 dws 输出格式：
      - 新版（扁平）：字段在事件顶层，type 即事件类型名，无 data 包裹
        {"type":"user_im_message_receive_o2o","sender":..,"content":..,
         "conversation_id":..,"message_id":..}
      - 旧版（嵌套）：外层 type=="event"，事件体在 data(二层 JSON 字符串).payload.body
        {"type":"event","event_type":..,"data":"{\"payload\":{\"body\":{..}}}"}
    """
    # etype：旧版在 event_type/event_key；新版事件类型名就是顶层 type
    etype = evt.get("event_type") or evt.get("event_key") or ""
    raw = evt.get("data")
    if raw:
        # 旧版嵌套格式：data 是二层 JSON 字符串，事件体在 payload.body
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
        quoted_msg_id = _quoted_msg_id(body)
        quoted_seq = _quoted_seq(body)
    else:
        # 新版扁平格式：字段直接在事件顶层（dws CLI 升级后的输出）
        etype = etype or evt.get("type") or ""
        sender = evt.get("sender", "未知发送人")
        content = (evt.get("content", "") or "").replace("\n", " ").strip()
        conv_id = evt.get("conversation_id", "")
        msg_id = evt.get("message_id", "")
        quoted_msg_id = _quoted_msg_id(evt)
        quoted_seq = _quoted_seq(evt)
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
    if quoted_msg_id:
        # 同样追加在尾部（理由同 atMention）。由 supervisor_review.classify_line 解析进
        # extra['quoted_msg_id'] —— core 的 parse_line 只认 atMention，不动它。
        tail += f" quotedMsgId={quoted_msg_id}"
        # 解析不出编号也要打标（`?`），让能力能区分"引用了审核消息但认不出号"和
        # "引用了完全无关的消息" —— 前者该追问，后者绝不能改判到别的待审上。
        tail += f" quotedSeq={quoted_seq or '?'}"
    return f"[connect] 收到 @{sender}: {content} ({tail})"


def main():
    _log("bridge 启动，等待 dws event NDJSON …")
    raw = 0        # 收到的原始事件行数（json 解析成功的）
    seen = 0       # 成功转成 connect-line 的条数
    warned = False # 格式告警只报一次
    # 用 readline 迭代而非 `for line in sys.stdin`：后者在 stdin 为管道时启用 readahead
    # 缓冲，低频事件流（单条消息几百字节、偶发）会卡在缓冲里迟迟不 yield，导致实时消息
    # 处理不了（bridge 看似"处理 0 条"）。readline 逐行读、行到即返回。
    for line in iter(sys.stdin.readline, ""):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            # 非事件行（如 dws 的状态 JSON），透传到 stderr 便于排查
            _log(f"跳过非 JSON 行: {line[:120]}")
            continue
        raw += 1
        # 不再硬过滤 type=="event"：新版 dws 扁平格式的 type 是事件类型名（如
        # user_im_message_receive_o2o），旧版才是 "event"。交给 _to_connect_line
        # 判定——能提取出 content 的才是消息事件，否则返回 None 自然跳过。
        out = _to_connect_line(evt)
        if out:
            print(out, flush=True)  # → CONNECT_LOG
            seen += 1
        # 格式健康检查：收到多条原始事件却一条都没解析出 → 大概率格式不匹配，报错一次。
        # 同时写 connect-log(stderr) 和 monitor.log，便于运维主动发现"静默失效"。
        if _should_warn_format(raw, seen, warned):
            warned = True
            msg = (f"⚠️ 格式健康告警：已收到 {raw} 条原始事件但成功解析 0 条 —— "
                   f"dws 输出格式疑似与 bridge 解析不匹配（dws 升级？），"
                   f"数字员工将收不到任何消息。样例行: {line[:200]}")
            _log(msg)
            _log_to_monitor(msg)
    _log(f"stdin 结束，共收到 {raw} 条原始事件、成功处理 {seen} 条")


if __name__ == "__main__":
    main()
