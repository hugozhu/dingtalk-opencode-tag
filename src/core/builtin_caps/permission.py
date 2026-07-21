"""permission — 工具授权审批能力（钉钉端批准 agent 的受限操作）

serve 端配了 ask 规则（如 opencode.jsonc `"permission": {"bash": "ask"}`）时，agent 调
受限工具会挂起该调用并发 permission SSE 事件。本能力把审批回程接到钉钉：
  1. on_sse_event：收 permission.asked（v1）/ permission.v2.asked（v2）→ 渲染操作详情
     （action / 目标 pattern / metadata 里的命令预览）→ 发到该 session 对应的**来源群**
     （事件只有 sessionID，用 brain.session_conv 反查 conv_id）。记 pending（含超时定时器）。
     同一请求两代事件都发时按 req_id 去重只发一次。
  2. on_inbound（优先级 15，先于 question 20 / text_reply 100）：存在 pending 时，用户
     回「同意」→ reply once（放行一次）；「总是」→ reply always（放行并存 saved permissions，
     同 pattern 下次免问）；「拒绝」→ reject。关键词严格匹配，未命中的文本放行给后续能力。
  3. 超时：CAP_PERMISSION_TIMEOUT（默认 60s）未答 → 自动 reject 解卡（agent 的阻塞
     message POST 随之返回错误让模型继续）。60s < brain 的 AGENT_OPENCODE_TIMEOUT(90s)，
     保证超时路径先于 brain HTTP 超时触发，任务能拿到明确的"权限被拒"而非整体超时。

回复端点按事件代际选择（serve 1.18.x 两代并存）：
  v1: POST /session/{sessionID}/permissions/{permissionID}  body={"response": r}
  v2: POST /permission/{requestID}/reply                    body={"reply": r}
另收 permission[.v2].replied 事件（本能力自己回的 / 别的客户端如 TUI 回的）→ 静默清
pending，避免重复审批与幽灵超时。

同步 brain 模型下链路成立性与 question 能力同构（见 question.py 模块注释 / #27）：
message POST 阻塞在 worker 线程，本能力在 log-tail 入站路径 POST reply 解卡。

开关：CAP_PERMISSION_ENABLED（默认开）。serve 未配 ask 规则时事件不会出现，本能力空转。
"""

import base64
import json
import os
import threading
import urllib.error
import urllib.request

from core.agent_common import find_serve_credentials, log
from core.capabilities import Capability, register
from core.inbound import KIND_TEXT
from core.brain import session_conv
from core.replier import send_reply

# 超时未答自动 reject 的秒数（安全网防会话卡死；须 < AGENT_OPENCODE_TIMEOUT）
_P_TIMEOUT = int(os.environ.get("CAP_PERMISSION_TIMEOUT", "60"))
# 审批关键词（lower 后严格匹配；未命中放行给后续能力，用户可能在说别的）
_ALLOW_KEYWORDS = {"同意", "允许", "放行", "approve", "allow", "yes", "y", "ok"}
_ALWAYS_KEYWORDS = {"总是", "总是允许", "一直允许", "always"}
_REJECT_KEYWORDS = {"拒绝", "不同意", "deny", "reject", "no"}

# pending 审批：req_id -> {sid, conv_id, conv_type, action, api("v1"|"v2"), timer}
_pending = {}
_pending_lock = threading.Lock()


# ---------------------------------------------------------------------------
# serve HTTP：按代际回复 once / always / reject
# ---------------------------------------------------------------------------

def _post_reply(req_id, sid, api, reply):
    """回复权限请求。返回 (ok, msg)。

    404 视为请求已不存在（别处已回 / serve 重启清了）——当作成功，别卡用户。
    """
    pid, port, pwd = find_serve_credentials()
    if not port:
        return False, "serve 凭据缺失"
    if api == "v2":
        url = f"http://127.0.0.1:{port}/permission/{req_id}/reply"
        body = {"reply": reply}
    else:
        url = f"http://127.0.0.1:{port}/session/{sid}/permissions/{req_id}"
        body = {"response": reply}
    headers = {"Content-Type": "application/json"}
    if pwd:
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"opencode:{pwd}".encode()).decode()
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return 200 <= r.status < 300, "ok"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True, "已失效(404)"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 事件归一化 + 渲染（纯函数，可单测）
# ---------------------------------------------------------------------------

def _normalize(event):
    """把两代 asked 事件归一成 {req_id, sid, api, action, resources, metadata}；非 asked 返回 None。"""
    etype = event.get("type", "")
    props = event.get("properties", {}) or {}
    if etype == "permission.v2.asked":
        return {"req_id": props.get("id"), "sid": props.get("sessionID", "") or "",
                "api": "v2", "action": props.get("action", "") or "",
                "resources": props.get("resources", []) or [],
                "metadata": props.get("metadata", {}) or {}}
    if etype == "permission.asked":
        return {"req_id": props.get("id"), "sid": props.get("sessionID", "") or "",
                "api": "v1", "action": props.get("permission", "") or "",
                "resources": props.get("patterns", []) or [],
                "metadata": props.get("metadata", {}) or {}}
    return None


def _render(p):
    """渲染审批消息（纯文本）。"""
    lines = ["🔐 需要授权", "", f"操作: {p['action'] or '(未知)'}"]
    for r in p["resources"][:5]:
        lines.append(f"目标: {r}")
    if len(p["resources"]) > 5:
        lines.append(f"… 等 {len(p['resources'])} 项")
    # metadata 里常见的可读字段（如 bash 的 command / 工具描述），截断防刷屏
    md = p.get("metadata", {}) or {}
    for key in ("command", "description", "title"):
        v = md.get(key)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            lines.append(f"{key}: {v[:200]}{'…' if len(v) > 200 else ''}")
    lines += ["",
              "💬 回「同意」放行一次 / 「总是」放行并记住 / 「拒绝」拒绝",
              f"⏰ {_P_TIMEOUT}s 未回自动拒绝"]
    return "\n".join(lines)


def _classify_reply(text):
    """把用户文本归类为 once/always/reject；未命中返回 None。"""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in _ALWAYS_KEYWORDS:
        return "always"
    if t in _ALLOW_KEYWORDS:
        return "once"
    if t in _REJECT_KEYWORDS:
        return "reject"
    return None


# ---------------------------------------------------------------------------
# pending 状态操作
# ---------------------------------------------------------------------------

def _pop(req_id):
    with _pending_lock:
        p = _pending.pop(req_id, None)
    if p and p.get("timer"):
        try:
            p["timer"].cancel()
        except Exception:
            pass
    return p


def _timeout(req_id):
    """超时未答：reject 解卡 + 通知来源群。"""
    p = _pop(req_id)
    if not p:
        return
    ok, msg = _post_reply(req_id, p["sid"], p["api"], "reject")
    log(f"permission 超时自动拒绝 req={req_id[:16]} ok={ok} {msg}")
    send_reply(p["conv_id"], p["conv_type"],
               f"⏰ 授权请求（{p['action']}）超时，已自动拒绝。")


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------

def on_sse_event(event, port, password):
    """收 asked → 发来源群 + 记 pending；收 replied → 静默清 pending。返回 True=已消费。"""
    etype = event.get("type", "")

    # 别处已回（含本能力自己回后 serve 广播）→ 清 pending 防重复审批/幽灵超时
    if etype in ("permission.replied", "permission.v2.replied"):
        req_id = (event.get("properties", {}) or {}).get("requestID", "")
        if req_id and _pop(req_id):
            log(f"permission 已在别处回复，清 pending req={req_id[:16]}")
            return True
        return False

    p = _normalize(event)
    if not p or not p["req_id"]:
        return False
    req_id = p["req_id"]
    with _pending_lock:
        dup = req_id in _pending
    if dup:
        return True   # 同一请求两代事件都发 → 只处理一次
    conv = session_conv(p["sid"]) or {}
    conv_id = conv.get("conv_id", "")
    conv_type = conv.get("conv_type", "2")
    if not conv_id:
        # 找不到来源群（非 brain 发起的 session）→ 不认领，走 core 默认
        log(f"permission: sid={p['sid'][:12]} 无来源群映射，跳过")
        return False

    timer = threading.Timer(_P_TIMEOUT, _timeout, args=(req_id,))
    with _pending_lock:
        _pending[req_id] = {
            "sid": p["sid"], "conv_id": conv_id, "conv_type": conv_type,
            "action": p["action"], "api": p["api"], "timer": timer,
        }
    timer.daemon = True
    timer.start()
    send_reply(conv_id, conv_type, _render(p))
    log(f"permission: 发起审批 req={req_id[:16]} sid={p['sid'][:12]} "
        f"action={p['action']} api={p['api']} → 群 {conv_id[:12]}")
    return True


def _find_pending_for_conv(conv_id):
    """找该群最早的 pending 审批（req_id, p）；无则 (None, None)。"""
    with _pending_lock:
        for req_id, p in _pending.items():
            if p["conv_id"] == conv_id:
                return req_id, p
    return None, None


def on_inbound(msg):
    """存在 pending 审批时截获群里的 同意/总是/拒绝；否则放行给后续能力。"""
    req_id, p = _find_pending_for_conv(msg.conv_id)
    if not req_id:
        return False  # 该群没有待审批请求，放行
    reply = _classify_reply(msg.text)
    if reply is None:
        return False  # 不是审批关键词（用户可能在说别的），放行
    popped = _pop(req_id)
    if not popped:
        return False  # 竞态：刚被超时/别处处理掉
    ok, m = _post_reply(req_id, popped["sid"], popped["api"], reply)
    verdict = {"once": "✅ 已放行（一次）", "always": "✅ 已放行并记住",
               "reject": "🚫 已拒绝"}[reply]
    log(f"permission 用户审批 req={req_id[:16]} reply={reply} ok={ok} {m}")
    if ok:
        send_reply(msg.conv_id, msg.conv_type, f"{verdict}：{popped['action']}")
    else:
        send_reply(msg.conv_id, msg.conv_type, f"⚠️ 审批提交失败：{m}")
    return True


# 测试用：清空 pending
def _reset():
    with _pending_lock:
        for p in _pending.values():
            if p.get("timer"):
                try:
                    p["timer"].cancel()
                except Exception:
                    pass
        _pending.clear()


CAPABILITY = Capability(
    name="permission",
    on_inbound=on_inbound,
    handles_kinds={KIND_TEXT},   # 审批回复是文本
    on_sse_event=on_sse_event,
    priority=15,                 # 先于 question20/image40/forward50/text100 看到用户回复
    default_enabled=True,
    loop_guard=True,             # 数字员工自己的确认消息不进审批匹配
)
register(CAPABILITY)
