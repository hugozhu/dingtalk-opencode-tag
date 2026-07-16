"""agent_common.py — 数字员工共享 Python 工具

提炼自: dingtalk-opencode-agent/agent_common.py (v4.1)
原作者: hugozhu

提供 6 类共享工具，被 event_watcher.py 和 handler_*.py 共用：

1. 常量（机器人身份 / profile / 超时）
2. 日志 & 通知（log / send_notification / _md）
3. dws/CLI 包装（_run_cli）
4. opencode serve 访问（find_serve_credentials / _find_bot_session /
   _create_session / _post_user_message / _get_message_text /
   _list_session_messages / _delete_session_message / _abort_and_clean_session /
   _find_session_with_predicate）
5. 视觉/多模态识别（_proxy_vision）
6. inject_and_forward 公共注入模板（find/create 会话 → post → get reply → send_notification）

纯工具——无模块级可变状态，无全局副作用。
"""

import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Config loading — config/config.local.json（真实值）覆盖占位默认
#
# 优先级（从高到低）: 环境变量 > config.local.json > config.example.json > 硬编码默认
# 这样 FDE 填了 config.local.json 就能真正生效（此前该文件从没被读取）。
# ---------------------------------------------------------------------------

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config")
# 项目根（运行时状态文件 .serve.port/.serve.pwd 等所在目录）
_PROJECT_ROOT = os.environ.get(
    "PROJECT_DIR",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _load_config_file():
    """读 config.local.json（优先）或 config.example.json，返回扁平 dict 或 {}。"""
    for name in ("config.local.json", "config.example.json"):
        path = os.path.join(_CONFIG_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


_CONFIG = _load_config_file()


def _cfg(env_key, *json_path, default=""):
    """取配置值：环境变量优先，然后 config.json 的嵌套路径，最后 default。"""
    if env_key and os.environ.get(env_key) is not None:
        return os.environ[env_key]
    node = _CONFIG
    for key in json_path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
        if node is None:
            return default
    return node if node is not None else default


# ---------------------------------------------------------------------------
# Constants — 环境变量 > config.local.json > 默认
# ---------------------------------------------------------------------------

# 机器人/数字员工身份（用于 send_notification 用机器人身份发消息）
ROBOT_CODE = _cfg("AGENT_ROBOT_CODE", "identity", "robot_code", default="your-robot-code")
USER_ID = _cfg("AGENT_USER_ID", "identity", "user_id", default="your-user-id")
PROFILE = _cfg("AGENT_PROFILE", "identity", "profile", default="default-profile")

# 视觉/多模态模型（经代理服务调用）
PROXY_URL = _cfg("PROXY_URL", "vision", "proxy_url", default="http://localhost:4000/v1")
PROXY_KEY = _cfg("PROXY_KEY", "vision", "proxy_key", default="sk-1234")
VISION_MODEL = _cfg("VISION_MODEL", "vision", "model", default="gemini-3.1-flash-image")

# session.directory 含此子串的就是数字员工用的会话（按 --agent-workdir 设置）
_BOT_DIR_SUBSTR = _cfg("AGENT_BOT_DIR_SUBSTR", "paths", "agent_workdir_basename",
                       default="your-agent-workdir")

# 注入模板轮询参数（测试 patch 为 0）
_INJECT_POLL_MAX_SECONDS = 60
_INJECT_POLL_INTERVAL = 5

# serve 凭据发现缓存（避免每次访问都全表 ps 扫描）
_CREDS_CACHE_TTL = float(os.environ.get("AGENT_CREDS_CACHE_TTL", "10"))
_creds_cache = {}
_creds_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Logging & Notification
# ---------------------------------------------------------------------------

def log(msg):
    """统一日志：时间戳 + 组件前缀，打到 stdout（由 launchd 落盘）"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [agent] {msg}", flush=True)


def send_notification(title, text):
    """通过 dws CLI 以机器人身份发消息到指定用户。

    用户实现可替换为别的通知后端（Slack/邮件/企业微信等）。
    """
    try:
        r = subprocess.run(
            ["dws", "chat", "message", "send-by-bot",
             "--robot-code", ROBOT_CODE, "--users", USER_ID,
             "--title", title[:60],
             "--text", text,
             "--profile", PROFILE, "--format", "markdown"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            log(f"send FAIL rc={r.returncode} title={title} stderr={r.stderr[:200]}")
            return False
        log(f"send OK title={title}")
        return True
    except Exception as e:
        log(f"send err: {e}")
        return False


def _md(title, status, body=""):
    """渲染简单的 markdown 消息（标题 + 粗体状态 + 可选正文）"""
    msg = f"### {title}\n\n**{status}**"
    if body:
        msg += f"\n\n{body}"
    return msg


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------

def _run_cli(args, timeout=60):
    """运行 CLI 子命令，带 profile。返回 (rc, stdout)。

    用于反查消息体（list-by-ids）、下载媒体、查群消息等。
    """
    cmd = ["dws"] + args
    if PROFILE:
        cmd += ["--profile", PROFILE]
    cmd += ["-y"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout.strip()
    except Exception as e:
        return -1, str(e)


# ---------------------------------------------------------------------------
# opencode serve access — 凭证/会话操作工具集
# ---------------------------------------------------------------------------

def find_serve_credentials():
    """从进程表定位 opencode serve 进程，返回 (pid, port, password) 或 (None, None, None)。

    通用思路: ps -ax 找 "<serve-cmd>" 进程 → 提取 --port + OPENCODE_SERVER_PASSWORD 环境变量

    带短 TTL 缓存（_CREDS_CACHE_TTL 秒）：agent_common 里 8 个函数各自调用本函数，
    一次 inject_and_forward 会触发 ≥3 次全表 ps。缓存命中时跳过 subprocess，
    大幅降低进程表扫描频率。缓存的是成功结果；失败不缓存（下次立即重试）。
    """
    now = time.time()
    with _creds_lock:
        cached = _creds_cache.get("value")
        if cached and (now - _creds_cache.get("ts", 0)) < _CREDS_CACHE_TTL:
            return cached

    creds = _discover_serve_credentials()
    if creds[1]:  # 只缓存成功结果（port 非空）
        with _creds_lock:
            _creds_cache["value"] = creds
            _creds_cache["ts"] = now
    return creds


def invalidate_serve_credentials():
    """清除凭据缓存（端口/密码变更、连接失败重连前调用）。"""
    with _creds_lock:
        _creds_cache.pop("value", None)
        _creds_cache.pop("ts", None)


def _discover_serve_credentials():
    """定位 serve 凭据。返回 (pid, port, password)。

    单一真相源：优先读 .serve.port / .serve.pwd 文件（start_serve 写、healthcheck 读），
    避免与进程表扫描出现两套真相导致漂移。文件缺失/不完整时回退到 ps 扫描，
    并把扫描结果写回文件，让 healthcheck 与本模块看到一致的凭据。
    """
    # 1. 优先从状态文件读（与 healthcheck check_serve_http 同源）
    port = _read_state_file(".serve.port")
    pwd = _read_state_file(".serve.pwd")
    if port and pwd:
        try:
            pid = int(_read_state_file(".serve.pid") or 0) or None
        except ValueError:
            pid = None
        try:
            return pid, int(port), pwd
        except ValueError:
            pass  # port 文件损坏，回退扫描

    # 2. 回退：进程表扫描
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid,args"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            if "agent-serve" not in line or "grep" in line:
                continue
            pid = line.split()[0]
            m = re.search(r"--port\s+(\d+)", line)
            port = int(m.group(1)) if m else None
            if pid and port:
                pr = subprocess.run(["ps", "eww", "-p", str(pid)], capture_output=True, text=True, timeout=5)
                # 通用：从环境变量提取 password（用户替换为自己的 serve 实现的环境变量名）
                pm = re.search(r"AGENT_SERVER_PASSWORD=(\S+)", pr.stdout)
                pwd = pm.group(1) if pm else None
                if pwd:
                    # 写回状态文件，保持与 healthcheck 同源
                    _write_state_file(".serve.pid", str(pid))
                    _write_state_file(".serve.port", str(port))
                    _write_state_file(".serve.pwd", pwd)
                    return int(pid), port, pwd
    except Exception as e:
        log(f"find serve err: {e}")
    return None, None, None


def _read_state_file(basename):
    """读 PROJECT_ROOT 下的运行时状态文件，返回 strip 后内容或 None。"""
    path = os.path.join(_PROJECT_ROOT, basename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    except Exception:
        return None


def _write_state_file(basename, value):
    """写运行时状态文件（best-effort，失败仅记日志）。"""
    path = os.path.join(_PROJECT_ROOT, basename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
    except Exception as e:
        log(f"write state {basename} err: {e}")


def _find_bot_session():
    """找数字员工当前会话（directory 含 _BOT_DIR_SUBSTR，time.updated 最新的）。

    注意：按 time.updated 倒序选最新活跃 session，**不**按 id 字典序——
    多个 session 共享同一 directory 时，id 字典序最大不等于最新活跃。
    """
    pid, port, pwd = find_serve_credentials()
    if not port:
        return None
    try:
        auth = base64.b64encode(f"opencode:{pwd}".encode()).decode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session",
            headers={"Authorization": f"Basic {auth}"})
        r = urllib.request.urlopen(req, timeout=8)
        data = json.loads(r.read().decode("utf-8"))
        sessions = data if isinstance(data, list) else data.get("data", [])
        bot = None
        bot_updated = 0
        for s in sessions:
            d = s.get("directory", "") or ""
            if _BOT_DIR_SUBSTR in d:
                updated = (s.get("time", {}) or {}).get("updated", 0) or 0
                if updated > bot_updated:
                    bot = s
                    bot_updated = updated
        return bot.get("id") if bot else None
    except Exception as e:
        log(f"find_bot_session err: {e}")
        return None


def _find_session_with_predicate(predicate, asked_ts_ms=None, max_candidates=8):
    """遍历 directory 匹配的 session，找含 predicate 匹配消息的最近活跃 session。

    通用版：predicate(message_dict) -> bool。用于找含特定 content 关键词的 session
    （如 dws dev connect 转发的原始 JSON 含 'msgtype=chatRecord'）。

    Args:
        predicate: callable(msg_dict) -> bool，True 即匹配
        asked_ts_ms: 可选时间戳（毫秒），匹配的消息 time.created >= asked_ts - 5s
        max_candidates: 最多扫描多少个候选 session（避免大列表全扫太慢）
    """
    pid, port, pwd = find_serve_credentials()
    if not port:
        return None
    try:
        auth = base64.b64encode(f"opencode:{pwd}".encode()).decode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session",
            headers={"Authorization": f"Basic {auth}"})
        r = urllib.request.urlopen(req, timeout=8)
        data = json.loads(r.read().decode("utf-8"))
        sessions = data if isinstance(data, list) else data.get("data", [])
        candidates = [s for s in sessions if _BOT_DIR_SUBSTR in (s.get("directory", "") or "")]
        candidates.sort(key=lambda s: (s.get("time", {}) or {}).get("updated", 0) or 0, reverse=True)

        threshold = (asked_ts_ms - 5000) if asked_ts_ms else 0
        for s in candidates[:max_candidates]:
            sid = s.get("id")
            if not sid:
                continue
            sess_msgs = _list_session_messages(sid)
            for m in sess_msgs:
                info = m.get("info", {}) or {}
                if threshold > 0:
                    created = (info.get("time", {}) or {}).get("created", 0) or 0
                    if not isinstance(created, (int, float)) or created < threshold:
                        continue
                if predicate(m):
                    return sid
        return None
    except Exception as e:
        log(f"find_session_with_predicate err: {e}")
        return None


def _create_session(title="agent-default"):
    """创建 opencode 会话，返回 full sid 或 None（兜底用）"""
    pid, port, pwd = find_serve_credentials()
    if not port:
        return None
    try:
        auth = base64.b64encode(f"opencode:{pwd}".encode()).decode()
        body = json.dumps({"title": title}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session",
            data=body,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            method="POST",
        )
        r = urllib.request.urlopen(req, timeout=10)
        d = json.loads(r.read().decode("utf-8"))
        sid = d.get("id")
        if sid:
            log(f"created session {sid} (title={title})")
        return sid
    except Exception as e:
        log(f"create_session err: {e}")
        return None


def _post_user_message(full_sid, text):
    """向会话发一条 user 消息（阻塞到该轮完成），返回 assistant 消息 id 或 None"""
    pid, port, pwd = find_serve_credentials()
    if not port:
        return None
    try:
        auth = base64.b64encode(f"opencode:{pwd}".encode()).decode()
        body = json.dumps({"input": text, "parts": [{"type": "text", "text": text}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session/{full_sid}/message",
            data=body,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            method="POST",
        )
        r = urllib.request.urlopen(req, timeout=180)
        d = json.loads(r.read().decode("utf-8"))
        return d.get("info", {}).get("id") or None
    except Exception as e:
        log(f"post_user_message err: {e}")
        return None


def _get_message_text(full_sid, msg_id):
    """取某条 assistant 消息的文本（拼接所有 text part）"""
    pid, port, pwd = find_serve_credentials()
    if not port:
        return ""
    try:
        auth = base64.b64encode(f"opencode:{pwd}".encode()).decode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session/{full_sid}/message",
            headers={"Authorization": f"Basic {auth}"})
        r = urllib.request.urlopen(req, timeout=8)
        data = json.loads(r.read().decode("utf-8"))
        msgs = data if isinstance(data, list) else data.get("data", [])
        for m in msgs:
            if m.get("info", {}).get("id") == msg_id:
                return "".join(p.get("text", "") for p in m.get("parts", []) if p.get("type") == "text")
        return ""
    except Exception as e:
        log(f"get_message_text err: {e}")
        return ""


def _list_session_messages(full_sid):
    """GET /session/{id}/message — 返回会话所有消息列表"""
    pid, port, pwd = find_serve_credentials()
    if not port:
        return []
    try:
        auth = base64.b64encode(f"opencode:{pwd}".encode()).decode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session/{full_sid}/message",
            headers={"Authorization": f"Basic {auth}"})
        r = urllib.request.urlopen(req, timeout=8)
        data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        log(f"list_session_messages err: {e}")
        return []


def _delete_session_message(full_sid, msg_id):
    """DELETE /session/{id}/message/{mid} — 删除单条消息"""
    pid, port, pwd = find_serve_credentials()
    if not port:
        return False
    try:
        auth = base64.b64encode(f"opencode:{pwd}".encode()).decode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session/{full_sid}/message/{msg_id}",
            headers={"Authorization": f"Basic {auth}"},
            method="DELETE",
        )
        r = urllib.request.urlopen(req, timeout=6)
        return r.status == 200
    except urllib.error.HTTPError as e:
        log(f"delete_session_message HTTP {e.code}")
        return False
    except Exception as e:
        log(f"delete_session_message err: {e}")
        return False


def _session_action(full_sid, action, body=None):
    """POST /session/{id}/{action} (abort/revert)。返回 True on HTTP 200。"""
    pid, port, pwd = find_serve_credentials()
    if not port:
        return False
    try:
        auth = base64.b64encode(f"opencode:{pwd}".encode()).decode()
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session/{full_sid}/{action}",
            data=data,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            method="POST",
        )
        r = urllib.request.urlopen(req, timeout=6)
        return r.status == 200
    except urllib.error.HTTPError as e:
        log(f"session_action {action} HTTP {e.code}")
        return False
    except Exception as e:
        log(f"session_action {action} err: {e}")
        return False


def _abort_and_clean_session(full_sid, asked_ts_ms=None):
    """abort 当前轮次 + DELETE asked_ts 之后到达的 user/assistant 消息。

    用于：依赖服务（如 dws dev connect）转发的原始 JSON 给 agent 时，agent 会基于
    JSON 生成无用回复。本函数清理这些"多余轮次"，避免污染后续注入的结构化 prompt。

    Returns: (aborted: bool, deleted_count: int)
    """
    # 1. 先 abort（阻止 LLM 继续生成；abort 触发的 finalizer 会被下面的 DELETE 清掉）
    aborted = _session_action(full_sid, "abort")
    log(f"abort_and_clean: abort sid={full_sid[:12]} ok={aborted}")

    # 2. DELETE 所有 asked_ts 之后的 user/assistant 消息
    deleted = 0
    msgs = _list_session_messages(full_sid)
    for m in msgs:
        info = m.get("info", {}) or {}
        role = info.get("role")
        msg_id = info.get("id")
        if not msg_id or role not in ("user", "assistant"):
            continue
        if asked_ts_ms is not None:
            created = (info.get("time", {}) or {}).get("created", 0) or 0
            if not isinstance(created, (int, float)) or created < asked_ts_ms:
                continue
        if _delete_session_message(full_sid, msg_id):
            deleted += 1
            log(f"abort_and_clean: DELETE {role} msg sid={full_sid[:12]} mid={str(msg_id)[:16]}")
        else:
            log(f"abort_and_clean: DELETE {role} msg failed sid={full_sid[:12]} mid={str(msg_id)[:16]}")

    log(f"abort_and_clean: sid={full_sid[:12]} aborted={aborted} deleted={deleted}")
    return aborted, deleted


# ---------------------------------------------------------------------------
# Vision / multimodal — via proxy
# ---------------------------------------------------------------------------

def _proxy_vision(image_bytes):
    """用多模态模型（经代理服务）识别图片，返回内容描述文本。

    prompt 改为逐字提取原文（保持顺序/换行/标点，不总结）。
    无文字图片则客观描述内容（场景/物体/UI 等），不做主观总结。
    """
    b64 = base64.b64encode(image_bytes).decode()
    body = json.dumps({
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": "请逐字提取这张图片中的所有文字内容（保持原始顺序、换行、标点，不要省略或总结）。如果图片中没有文字，则客观描述图片内容（场景、物体、UI 元素、图表数据等），不要做主观总结或解读。"},
        ]}],
    }).encode()
    try:
        req = urllib.request.Request(
            f"{PROXY_URL}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {PROXY_KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        r = urllib.request.urlopen(req, timeout=60)
        d = json.loads(r.read().decode("utf-8"))
        return (d.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        log(f"vision err: {e}")
        return ""


# ---------------------------------------------------------------------------
# inject_and_forward — 公共注入模板
#
# 两个 handler（如 handle_image / handle_forward）历史上重复：
#   sid = _find_bot_session()
#   if not sid: sid = _create_session(title)
#   if not sid: send_notification(...); return
#   aid = _post_user_message(sid, prompt)
#   reply = _get_message_text(sid, aid) if aid else ""
#   if reply: send_notification(reply_title, reply_md)
#   else:     send_notification(no_reply_title, no_reply_md)
#
# 差异点：session_title / reply 通知消息（可能多条）/ no-session / no-reply 兜底
# 解决方案：用 callable 参数化，handler 自己构造消息
# ---------------------------------------------------------------------------

def inject_and_forward(prompt, *, session_title, make_reply_msgs,
                       make_no_session_msg, make_no_reply_msg):
    """Find/create bot session → inject prompt → fetch reply → send notification.

    Args:
        prompt: text to inject into the agent session.
        session_title: title passed to _create_session as fallback.
        make_reply_msgs: callable(reply_text) -> list[(title, md_text)]   # 允许多条
        make_no_session_msg: callable() -> (title, md_text)
        make_no_reply_msg: callable() -> (title, md_text)

    Returns reply text on success, or None on failure (no session or no reply).
    """
    sid = _find_bot_session()
    if not sid:
        sid = _create_session(session_title)
    if not sid:
        title, md = make_no_session_msg()
        send_notification(title, md)
        return None
    aid = _post_user_message(sid, prompt)
    reply = _get_message_text(sid, aid) if aid else ""
    if not reply:
        title, md = make_no_reply_msg()
        send_notification(title, md)
        return None
    for title, md in make_reply_msgs(reply):
        send_notification(title, md)
    return reply
