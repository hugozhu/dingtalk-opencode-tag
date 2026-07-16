"""event_watcher.py — 数字员工事件流监听主进程

提炼自: dingtalk-opencode-agent/event-watcher.py (v8+)
原作者: hugozhu

5 大职责（被 launchd 托管的 monitor.sh 拉起，与主 connect 进程并列）:

1. SSE 事件流监听（opencode serve /event）：
   - 无限重试 + 退避（MIN→MAX_RECONNECT_INTERVAL）
   - serve 未启动时等待而非退出
   - 端口变更自动切换
   - 凭据每次实时从 find_serve_credentials() 获取
   - 事件 → format_and_forward() 转发到通知渠道

2. log-tail 监听（opencode-connect.log）：
   - tail -F + inode 检测轮转
   - 解析 "[connect] 收到 @user:" 行 → 匹配业务 handler（reply/forward/image）
   - 跨行格式检测状态机（处理两行格式的消息）
   - 线程安全的 dedup（_seen 集合 + _state_lock）
   - 匹配后 spawn 业务 handler daemon thread

3. 状态机 cleanup（处理 spurious 多余轮次）：
   - core 只做 TTL 过期兜底（CLEANUP_TTL=40s）+ 提供 route_cleanup_state hook
   - 具体 awaiting_spurious → cleaning 状态机由 custom.routes 实现（业务特定）
   - DELETE + abort 多余 user/assistant 消息
   - 多选/连发场景下累积删除 expected_count 条

4. 撤回空回复尝试（监听 "agent 已生成回复" + "普通消息已发送"）：
   - 检测到空 finalizer（"本地 agent 无文本输出"）→ 主动 list + recall
   - 已知限制：钉钉 API "仅消息发送者可撤回" + 缺 processQueryKey，部分场景走不通

5. /reboot 远程指令：
   - log-tail 检测 "/reboot" 文本 → 派生 reboot.sh + os._exit(0)
   - 60s 冷却防连发
"""

import base64
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# src/ 加进 sys.path，支持 package 风格 import（core / custom / templates）
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
PROJECT_ROOT = os.path.dirname(SRC_DIR)

from core.agent_common import (
    PROFILE,
    _create_session,
    _find_bot_session,
    _get_message_text,
    _md,
    _post_user_message,
    _proxy_vision,
    find_serve_credentials,
    inject_and_forward,
    log,
    send_notification,
)
from custom.routes import (
    route_reply,
    route_business_line,
    route_sse_event,
    route_cleanup_state,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_RECONNECT_INTERVAL = 3
MAX_RECONNECT_INTERVAL = 30

# 主进程运行日志 + connect 进程日志（log-tail 监听这个）
CONNECT_LOG = os.environ.get("CONNECT_LOG", os.path.join(PROJECT_ROOT, "agent-connect.log"))

# log-tail 正则：匹配 "[connect] 收到 @user: text (convType=N convId=...)"
# 用户根据自己 connect 的日志格式调整
REPLY_RE = re.compile(r'\[connect\] 收到 @(.+?):\s*(.+?)\s+\(convType=(\d+)')

# 撤回空回复相关
AGENT_REPLY_RE = re.compile(r'\[connect\] agent 已生成回复 \([^)]+ [\d.]+s\): (.*)')
MSG_SENT_RE = re.compile(r'\[connect\] 普通消息已发送 \([^,]+, msgId=([^)]+)\)')
EMPTY_REPLY_MARKERS = ("本地 agent 无文本输出", "本地 agent 无文本", "无文本输出")
_last_agent_reply_text = ""
_last_agent_reply_lock = threading.Lock()

# /reboot 冷却
REBOOT_COOLDOWN = 60
last_reboot_at = 0.0
reboot_lock = threading.Lock()

running = True
sse_lock = threading.Lock()
recent_send = {}
RECENT_TTL = 5

session_start_time = {}
session_usage = {}    # sid -> (cost, tokens)
session_model = {}    # sid -> str
turn_count = {}       # sid -> int
turn_seen = {}        # sid -> set(messageID)

# 状态机 cleanup：sid(12) -> {state, asked_ts, reply_texts, expected_count,
#                              deleted_ids, deleted_msgs, expires}
cleanup_state = {}
cleanup_lock = threading.Lock()
CLEANUP_TTL = 40


# ---------------------------------------------------------------------------
# SSE event stream parsing
# ---------------------------------------------------------------------------

def parse_sse_events(sock):
    """从 socket 读 SSE 流，yield data 字段。"""
    buf = ""
    while running:
        try:
            sock.settimeout(5)
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")

            while "\r\n" in buf:
                idx = buf.index("\r\n")
                len_line = buf[:idx].strip()
                if not len_line:
                    buf = buf[idx+2:]
                    continue
                try:
                    chunk_size = int(len_line, 16)
                except ValueError:
                    buf = buf[idx+2:]
                    continue

                chunk_data_start = idx + 2
                chunk_data_end = chunk_data_start + chunk_size
                if len(buf) < chunk_data_end + 2:
                    break

                chunk_body = buf[chunk_data_start:chunk_data_end]
                buf = buf[chunk_data_end + 2:]

                for line in chunk_body.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("data: "):
                        yield stripped[6:]
                    elif stripped.startswith("data:"):
                        yield stripped[5:]
        except socket.timeout:
            continue
        except Exception as e:
            log(f"parse err: {e}")
            break


# ---------------------------------------------------------------------------
# format_and_forward — SSE 事件 → 通知渠道
# 用户在这里实现自己的事件转发逻辑
# ---------------------------------------------------------------------------

def format_and_forward(event, port=None, password=None):
    """处理一个 SSE 事件，决定是否转发到通知渠道。

    Returns True if forwarded, False otherwise.
    用户在这里实现自己的事件处理逻辑。
    """
    etype = event.get("type", "")
    props = event.get("properties", {})
    full_sid = props.get("sessionID", "") or ""
    sid = full_sid[:12]

    # ---- 状态机 cleanup 处理（spurious 多余轮次清理）----
    # core 只做 TTL 过期兜底 + 提供 hook；具体 awaiting_spurious → cleaning 状态机
    # 由 custom.routes.route_cleanup_state 实现（业务特定，FDE 在 custom 层写）。
    with cleanup_lock:
        cs = cleanup_state.get(sid)
        if cs and time.time() > cs.get("expires", 0):
            cleanup_state.pop(sid, None)
            log(f"cleanup 过期清理 sid={sid}")
            cs = None
    # 下放给 custom：返回 True 表示 custom 已消费该事件（core 不再默认转发）
    if route_cleanup_state(event, cleanup_state, cleanup_lock):
        return True

    # ---- 收到新请求：session 变 busy 时第一时间通知 ----
    if etype == "session.status" and sid:
        status = props.get("status", {})
        if status.get("type") == "busy" and sid not in session_start_time:
            session_start_time[sid] = time.time()
            send_notification("📥 收到新请求",
                              _md("收到新请求", f"📞 会话 **{sid}** 已开始处理", ""))
            return True

    # ---- 会话完成通知 ----
    if etype == "session.idle":
        lines = []
        if sid in session_start_time:
            elapsed = time.time() - session_start_time[sid]
            lines.append(f"**耗时:** {elapsed:.1f}s")
            del session_start_time[sid]
        body = "\n".join(f"- {l}" for l in lines) if lines else ""
        send_notification("✅ 会话完成",
                          _md("会话完成", f"✅ 会话 **{sid}** 已完成", body))
        # 清理 sid 相关状态
        session_usage.pop(sid, None)
        session_model.pop(sid, None)
        turn_count.pop(sid, None)
        turn_seen.pop(sid, None)
        return True

    return False


# ---------------------------------------------------------------------------
# Log-tail thread — 监听 connect 日志，触发业务 handler
# ---------------------------------------------------------------------------

def _recall_empty_reply(_unused_msg_id=None):
    """撤回依赖服务（如 dws dev connect）发到通知渠道的空回复。

    通用思路：检测到"agent 已生成回复: 空回复标记"时，主动 list 最近消息找
    含空回复标记的最新一条，调 recall 撤回。

    已知限制：钉钉 API "仅消息发送者可撤回"，用用户身份撤回不了机器人发的消息；
    dws dev connect 不暴露 processQueryKey 导致 recall-by-bot 也走不通。
    需要依赖服务端配合过滤空回复才能彻底解决。
    """
    # 用户实现：用 dws chat message list 找空回复 + recall
    log("recall: 检测到空回复，撤回逻辑由用户实现")


def handle_reboot(user):
    """收到 /reboot 指令：发通知 → 派生 reboot.sh → os._exit(0)。"""
    global last_reboot_at
    now = time.time()
    with reboot_lock:
        if now - last_reboot_at < REBOOT_COOLDOWN:
            log(f"reboot 冷却中，忽略 {user} 的 /reboot")
            send_notification("⏳ 忽略重复 /reboot",
                              _md("忽略", "⏳ 冷却中",
                                  f"请 {int(REBOOT_COOLDOWN - (now - last_reboot_at))}s 后再试"))
            return
        last_reboot_at = now

    log(f"reboot: 收到 {user} 的 /reboot 指令，派生 reboot.sh 并退出")
    send_notification("🔄 正在重启",
                      _md("正在重启", "🔄 收到 /reboot 指令",
                          f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n- 约 10s 后恢复"))

    try:
        subprocess.Popen(["bash", os.path.join(PROJECT_ROOT, "bin", "core", "reboot.sh")],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        log(f"reboot: 派生 reboot.sh 失败: {e}")
        send_notification("⚠️ 重启失败",
                          _md("重启失败", f"⚠️ 无法派生 reboot.sh: {e}", ""))
        return

    os._exit(0)


def log_tail_thread(stop_flag):
    """tail connect 日志，解析用户回复，喂给业务 handler。

    用户在这里实现自己的业务路由：
    - 文本回复 → handle_reply(user, text)
    - 图片 → handle_image(detected_at)
    - 合并转发 → handler_template.handle_forward(msg_id, convs)
    - /reboot → handle_reboot(user)
    - 空回复检测 + 撤回
    """
    global _last_agent_reply_text
    log(f"log-tail 启动，监听 {CONNECT_LOG}")
    pos = None
    inode = None
    while running and not stop_flag.is_set():
        try:
            f = open(CONNECT_LOG, "r", encoding="utf-8", errors="replace")
            try:
                cur_inode = os.fstat(f.fileno()).st_ino
            except Exception:
                cur_inode = None
            if pos is None or inode != cur_inode:
                f.seek(0, 2)
                pos = f.tell()
                inode = cur_inode
            else:
                f.seek(pos)
            while running and not stop_flag.is_set():
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    try:
                        st = os.stat(CONNECT_LOG)
                        if st.st_ino != cur_inode or st.st_size < f.tell():
                            break
                    except OSError:
                        break
                    continue
                pos = f.tell()

                # --- 撤回空回复：解析"agent 已生成回复" + "普通消息已发送"---
                arm = AGENT_REPLY_RE.search(line)
                if arm:
                    with _last_agent_reply_lock:
                        _last_agent_reply_text = arm.group(1).strip()
                msent = MSG_SENT_RE.search(line)
                if msent:
                    with _last_agent_reply_lock:
                        last_text = _last_agent_reply_text
                        _last_agent_reply_text = ""
                    if last_text and any(mk in last_text for mk in EMPTY_REPLY_MARKERS):
                        log(f"recall: 检测到空回复 last_text={last_text[:50]!r}，派生 recall 线程")
                        threading.Thread(target=_recall_empty_reply, daemon=True).start()

                # --- 业务路由：调用 custom.routes 注册的处理逻辑（FDE 不改这里）---
                m = REPLY_RE.search(line)
                if m:
                    user, text, conv_type = m.group(1), m.group(2).strip(), m.group(3)
                    if text.lower() == "/reboot":
                        log(f"reboot: 命中 /reboot 指令 user={user}")
                        threading.Thread(target=handle_reboot, args=(user,), daemon=True).start()
                    else:
                        route_reply(user, text, conv_type, line)
                # 业务消息行路由（合并转发 / 业务特殊消息等）
                route_business_line(line)
            try:
                f.close()
            except Exception:
                pass
        except FileNotFoundError:
            time.sleep(2)
        except Exception as e:
            log(f"log-tail err: {e}")
            time.sleep(2)
        time.sleep(1)


# ---------------------------------------------------------------------------
# SSE connection — infinite retry + backoff
# ---------------------------------------------------------------------------

def connect_sse():
    """连接 opencode serve SSE /event 流，无限重试 + 退避。"""
    interval = MIN_RECONNECT_INTERVAL
    while running:
        try:
            pid, port, pwd = find_serve_credentials()
            if not port:
                log(f"等待 serve 启动... ({interval}s)")
                time.sleep(interval)
                interval = min(interval * 2, MAX_RECONNECT_INTERVAL)
                continue
            auth = base64.b64encode(f"opencode:{pwd}".encode()).decode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/event",
                headers={"Authorization": f"Basic {auth}", "Accept": "text/event-stream"})
            r = urllib.request.urlopen(req, timeout=10)
            log(f"SSE 已连接 serve port={port}")
            interval = MIN_RECONNECT_INTERVAL
            sock = r.fp.raw._sock  # 取底层 socket
            for data in parse_sse_events(sock):
                if not running:
                    break
                try:
                    event = json.loads(data)
                    with sse_lock:
                        # SSE 事件路由：custom 可拦截，返回 False 走 core 默认转发
                        if not route_sse_event(event, port, pwd):
                            format_and_forward(event, port=port, password=pwd)
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    log(f"format_and_forward err: {e}")
        except Exception as e:
            log(f"SSE 连接失败: {e}，{interval}s 后重试")
            # 连接失败可能是 serve 重启换了端口/密码，清缓存下次重新发现
            from core.agent_common import invalidate_serve_credentials
            invalidate_serve_credentials()
            time.sleep(interval)
            interval = min(interval * 2, MAX_RECONNECT_INTERVAL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log(f"event-watcher 启动 - 无限重试 + 自动重连")
    stop_flag = threading.Event()

    # 启动 log-tail 线程
    t = threading.Thread(target=log_tail_thread, args=(stop_flag,), daemon=True)
    t.start()

    # 主线程跑 SSE 连接
    try:
        connect_sse()
    except KeyboardInterrupt:
        log("收到 KeyboardInterrupt，退出")
    finally:
        stop_flag.set()
        running = False


if __name__ == "__main__":
    main()
