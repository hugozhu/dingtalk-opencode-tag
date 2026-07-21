"""brain.py — 数字员工的"大脑"：把用户消息生成回复文本（custom 层）

可插拔后端，由环境变量 AGENT_BRAIN 选择：
  echo     (默认)  零依赖，规则式回复。用于打通收发闭环、无网络/无 LLM 也能跑。
  opencode         调 opencode 生成回复。**优先走本机 opencode serve 的 HTTP 接口**
                   （复用常驻进程，省掉每次 `opencode run` 的冷启动，实测快 ~3x）；
                   serve 不可用时**自动回退**到 `opencode run` 一次性子进程，保证
                   serve 挂了也永远有回复。免鉴权可用 opencode/*-free 模型。
  proxy            经 agent_common.PROXY_URL 调用 LLM /chat/completions 生成回复。

为什么默认 echo：本机未必装 opencode / LLM proxy 未必可达。默认走 echo 保证 pipeline
今天就能端到端验证；配好后设 AGENT_BRAIN=opencode 或 proxy 即切换。

调试：AGENT_DEBUG=1 时，每次 opencode 调用（HTTP 与 CLI 两条路）单独记一条到
opencode.log（默认 <项目根>/opencode.log，可用 AGENT_OPENCODE_LOG 覆盖）：
transport / model / 耗时 / prompt+reply 长度 / reply 预览 / 成败。错误恒记，不受开关影响。

会话连续性（#56）：默认无状态（每条消息新建 session 即删）；设 AGENT_SESSION_REUSE=1 后
同一 conv 复用 serve session 带多轮上下文（TTL 过期 + LRU 逐出 + 重置关键词断上下文）。

接口：generate_reply(user, text, ctx=None) -> str（返回空串表示不回复）
"""

import base64
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from core.agent_common import PROXY_URL, PROXY_KEY, find_serve_credentials, log

# 大脑后端选择
_BRAIN = os.environ.get("AGENT_BRAIN", "echo")
# proxy 后端用的对话模型（区别于 VISION_MODEL）
_CHAT_MODEL = os.environ.get("AGENT_CHAT_MODEL", "gpt-4o-mini")
# opencode 后端用的模型（provider/model 格式；免鉴权可用 *-free）
_OPENCODE_MODEL = os.environ.get("AGENT_OPENCODE_MODEL", "opencode/deepseek-v4-flash-free")
_OPENCODE_BIN = os.environ.get("AGENT_OPENCODE_BIN", "opencode")
_OPENCODE_TIMEOUT = int(os.environ.get("AGENT_OPENCODE_TIMEOUT", "90"))

# 会话连续性（#56）：同一 conv_id 复用同一个 serve session，多轮历史由 serve 自带。
#   AGENT_SESSION_REUSE   缺省开启（项目默认）；设 0 或空串回退旧的无状态语义（每条消息新建即删）。
#   AGENT_SESSION_TTL     会话闲置多少秒后过期重建（默认 1800=30min）。
#   AGENT_SESSION_MAX     最多同时保活多少个 conv 的 session（LRU 逐出，默认 64）。
#   AGENT_SESSION_RESET_KEYWORDS  触发主动断上下文（删旧 session 重建）的整句关键词，逗号分隔。
_SESSION_REUSE = os.environ.get("AGENT_SESSION_REUSE", "1") in ("1", "true", "True", "yes", "on")
_SESSION_TTL = int(os.environ.get("AGENT_SESSION_TTL", "1800"))
_SESSION_MAX = int(os.environ.get("AGENT_SESSION_MAX", "64"))
_RESET_KEYWORDS = {
    k.strip().lower()
    for k in os.environ.get("AGENT_SESSION_RESET_KEYWORDS", "/new,新话题,重新开始,清空上下文").split(",")
    if k.strip()
}


# per-session 权限规则（JSON 数组，serve v1 格式 [{"permission","pattern","action"}]）。
# 配了 ask 规则时命中的工具调用会挂起并发 permission.asked SSE 事件 → permission 能力
# 把审批路由到钉钉来源群（回「同意/总是/拒绝」，超时自动拒绝）。空=不传，serve 用自身
# 默认（无全局配置时全放行）。只作用于 HTTP 路径的临时 session；CLI 回退路径不受控。
def _parse_permission(raw):
    """解析 AGENT_OPENCODE_PERMISSION；非法 JSON / 非数组时告警并忽略（返回 None）。"""
    if not (raw or "").strip():
        return None
    try:
        rules = json.loads(raw)
    except ValueError:
        log("AGENT_OPENCODE_PERMISSION 不是合法 JSON，忽略")
        return None
    if not isinstance(rules, list):
        log("AGENT_OPENCODE_PERMISSION 应为规则数组，忽略")
        return None
    return rules or None


_OPENCODE_PERMISSION = _parse_permission(os.environ.get("AGENT_OPENCODE_PERMISSION", ""))

# 调试开关 + opencode 调用独立日志（与 agent_common 的 AGENT_DEBUG 语义一致）
_DEBUG = os.environ.get("AGENT_DEBUG", "") in ("1", "true", "True")
_PROJECT_ROOT = os.environ.get(
    "PROJECT_DIR",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_OPENCODE_LOG = os.environ.get(
    "AGENT_OPENCODE_LOG", os.path.join(_PROJECT_ROOT, "opencode.log"))

# 临时 session 登记表已上浮到 core.brain（纯机制，供 text_reply 抑制 SSE 通知 +
# question 回程路由）。这里 re-export，保持本模块内 _register_textreply_sid 等调用不变，
# 且能力 `from custom.brain import ...` 向后兼容。
from core.brain import (                                # noqa: E402
    register_session as _register_textreply_sid,
    is_textreply_session,
    session_conv,
)
# 系统提示词（proxy/opencode 后端），可用环境变量覆盖
_SYSTEM_PROMPT = os.environ.get(
    "AGENT_SYSTEM_PROMPT",
    "你是一个数字员工助手，在钉钉群里回答同事的问题。回答简洁、准确、友好，用中文。",
)
# 回复长度上限（防止刷屏）
_MAX_REPLY_CHARS = int(os.environ.get("AGENT_MAX_REPLY_CHARS", "1000"))


def _oc_log(transport, model, elapsed, prompt, reply, ok, err=""):
    """把一次 opencode 调用记到独立 opencode.log。

    成功记录仅在 AGENT_DEBUG=1 时写；失败（ok=False）恒记，不受开关影响，
    便于事后排查"回复为空/超时"到底断在 HTTP 还是 CLI。best-effort，写失败静默。
    """
    if ok and not _DEBUG:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    preview = (reply or "").replace("\n", " ")[:80]
    line = (f"[{ts}] transport={transport} model={model} "
            f"elapsed={elapsed:.2f}s prompt_len={len(prompt or '')} "
            f"reply_len={len(reply or '')} ok={ok}")
    if err:
        line += f" err={err[:160]!r}"
    if preview:
        line += f" reply={preview!r}"
    try:
        with open(_OPENCODE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # 调试日志写失败不影响主流程


def _split_model(model):
    """把 'provider/model' 拆成 (providerID, modelID)。无 '/' 时 provider 空串。"""
    if "/" in (model or ""):
        provider, _, mid = model.partition("/")
        return provider, mid
    return "", (model or "")


# ---------------------------------------------------------------------------
# 会话复用表（#56）：conv_id -> {sid, last, lock}
# ---------------------------------------------------------------------------
# LRU（OrderedDict，命中/新建移到末尾，超上限从头逐出）+ 闲置 TTL 过期。每 conv 一把锁，
# 保证同一会话先后到达的消息串行走同一 session（serve 对 busy session 的并发 POST 未保证
# 有序）。不同 conv 并行不受影响。CLI 回退路径不参与复用（拿不到 serve session，降级无状态）。
from collections import OrderedDict                       # noqa: E402

_conv_sessions = OrderedDict()   # conv_id -> {"sid": str, "last": float}
_conv_locks = {}                 # conv_id -> threading.Lock（保护单会话内的顺序）
_conv_meta_lock = threading.Lock()   # 保护上面两张表本身的结构性改动


def _conv_lock(conv_id):
    """取某会话的串行锁（不存在则建）。"""
    with _conv_meta_lock:
        lk = _conv_locks.get(conv_id)
        if lk is None:
            lk = _conv_locks[conv_id] = threading.Lock()
        return lk


def _lookup_sid(conv_id):
    """查该 conv 未过期的 sid；过期/无则返回 None（过期项顺手删除）。"""
    if not conv_id:
        return None
    with _conv_meta_lock:
        rec = _conv_sessions.get(conv_id)
        if not rec:
            return None
        if time.time() - rec["last"] > _SESSION_TTL:
            _conv_sessions.pop(conv_id, None)   # 过期 → 丢弃，调用方重建
            return None
        _conv_sessions.move_to_end(conv_id)     # LRU：命中移到末尾
        return rec["sid"]


def _remember_sid(conv_id, sid):
    """登记/刷新 conv→sid，并做 LRU 逐出。返回被逐出的 (conv_id, sid) 列表供删远端 session。"""
    evicted = []
    if not conv_id or not sid:
        return evicted
    with _conv_meta_lock:
        _conv_sessions[conv_id] = {"sid": sid, "last": time.time()}
        _conv_sessions.move_to_end(conv_id)
        while len(_conv_sessions) > _SESSION_MAX:
            old_cid, old_rec = _conv_sessions.popitem(last=False)
            evicted.append((old_cid, old_rec["sid"]))
    return evicted


def _forget_sid(conv_id):
    """删除某 conv 的复用记录，返回其旧 sid（无则 None）。用于 /new 重置 + 404 失效。"""
    if not conv_id:
        return None
    with _conv_meta_lock:
        rec = _conv_sessions.pop(conv_id, None)
        return rec["sid"] if rec else None


def _is_reset(text):
    """整句命中重置关键词（大小写不敏感）→ 主动断上下文。"""
    return (text or "").strip().lower() in _RESET_KEYWORDS


def _reset_sessions():
    """清空复用表（测试用）。"""
    with _conv_meta_lock:
        _conv_sessions.clear()
        _conv_locks.clear()



def generate_reply(user, text, ctx=None, raw=False):
    """根据用户消息生成回复文本。返回空串 = 不回复。

    Args:
        user: 发送者展示名
        text: 消息正文（已 strip）
        ctx:  可选上下文 dict（conv_id / msg_id / conv_type 等）
        raw:  True 时 text 已是**完整 prompt**，后端不再拼 "{user}：" 前缀
              （合并转发等已自行组装结构化 prompt 的调用方用它，避免前缀污染上下文）
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        if _BRAIN == "proxy":
            reply = _brain_proxy(user, text, ctx, raw=raw)
        elif _BRAIN == "opencode":
            reply = _brain_opencode(user, text, ctx, raw=raw)
        else:
            reply = _brain_echo(user, text, ctx)
    except Exception as e:
        log(f"brain({_BRAIN}) err: {e}")
        reply = ""
    if reply and len(reply) > _MAX_REPLY_CHARS:
        reply = reply[:_MAX_REPLY_CHARS] + "…（已截断）"
    return reply


# ---------------------------------------------------------------------------
# echo 后端 — 零依赖规则式
# ---------------------------------------------------------------------------

def _brain_echo(user, text, ctx):
    """规则式回复：支持简单指令 + 默认回声。无网络依赖。"""
    low = text.lower()
    if low in ("ping", "在吗", "在不在"):
        return "在的，有什么可以帮你？"
    if low in ("help", "帮助", "/help"):
        return ("我是数字员工（echo 模式）。当前会复述你的消息；"
                "配置 AGENT_BRAIN=proxy 后可接入 LLM 智能回复。")
    if low.startswith(("你好", "hi", "hello", "您好")):
        return f"你好 {user}！我是数字员工，很高兴为你服务。"
    # 默认：复述，证明收发闭环通了
    return f"收到你的消息：{text}"


# ---------------------------------------------------------------------------
# opencode 后端 — HTTP 优先（serve 常驻，快）+ CLI 回退（serve 挂了也有回复）
# ---------------------------------------------------------------------------

def _brain_opencode(user, text, ctx, raw=False):
    """opencode 大脑：优先走 serve HTTP，serve 不可用/出错时回退 `opencode run` CLI。

    HTTP 复用常驻 serve 进程，省掉每次 CLI 冷启动（实测 ~3x）；serve 未起/凭据缺失/
    请求异常时无缝回退到一次性子进程，保证收发闭环永远有回复。

    会话连续性（#56）：开启 AGENT_SESSION_REUSE 时，同一 conv 复用 serve session 带多轮
    上下文；用户发重置关键词（/new 等）→ 断上下文重建，不打扰模型直接回确认。
    """
    conv_id = (ctx or {}).get("conv_id", "")
    # 重置指令：仅在复用模式下有意义（无状态模式每条本就是新会话）
    if _SESSION_REUSE and conv_id and _is_reset(text):
        old = _forget_sid(conv_id)
        if old:
            pid, port, pwd = find_serve_credentials()
            if port:
                _delete_session(port, pwd, old)
        return "🆕 已开启新话题，之前的上下文已清空。"

    prompt = text if raw else f"{user}：{text}"
    reply = _brain_opencode_http(prompt, ctx=ctx)
    if reply is not None:
        return reply
    # HTTP 不可用（serve 没起/凭据缺失/异常）→ 回退 CLI（无状态，拿不到 serve session）
    log("brain(opencode): serve HTTP 不可用，回退 opencode run CLI")
    return _brain_opencode_cli(prompt)


def _serve_request(method, port, pwd, path, body=None, timeout=8):
    """对 opencode serve 发一个 HTTP 请求，返回解析后的 JSON（无 body 时返回 None）。

    serve 设了 OPENCODE_SERVER_PASSWORD 时用 Basic auth(opencode:<pwd>)；未设(pwd 空)
    则不带鉴权头。与 agent_common / healthcheck 的鉴权约定一致。
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if pwd:
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"opencode:{pwd}".encode()).decode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    r = urllib.request.urlopen(req, timeout=timeout)
    raw = r.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else None


def _create_session(port, pwd):
    """建一个 serve session（带可选 per-session 权限规则）。返回 sid 或抛错。"""
    body = {"title": "agent-textreply"}
    if _OPENCODE_PERMISSION:
        body["permission"] = _OPENCODE_PERMISSION
    created = _serve_request("POST", port, pwd, "/session", body, timeout=10)
    sid = (created or {}).get("id")
    if not sid:
        raise RuntimeError("create session 无 id")
    return sid


def _delete_session(port, pwd, sid):
    """best-effort 删除 serve session（失败静默，不影响主流程）。"""
    if not sid:
        return
    try:
        _serve_request("DELETE", port, pwd, f"/session/{sid}", timeout=6)
    except Exception:
        pass


def _post_message(port, pwd, sid, prompt, provider, model_id):
    """向 session 发一条 message，拼接 text parts 返回回复文本。"""
    d = _serve_request(
        "POST", port, pwd, f"/session/{sid}/message",
        {
            "model": {"providerID": provider, "modelID": model_id},
            "system": _SYSTEM_PROMPT,
            "parts": [{"type": "text", "text": prompt}],
        },
        timeout=_OPENCODE_TIMEOUT,
    ) or {}
    return "".join(
        p.get("text", "") for p in d.get("parts", []) if p.get("type") == "text"
    ).strip()


def _brain_opencode_http(prompt, ctx=None):
    """走 opencode serve HTTP 生成回复。

    两种会话语义（AGENT_SESSION_REUSE 开关）：
      - 关（默认，旧语义）：每条消息建临时 session → POST message → 删 session。无状态、
        互不污染，但没有跨消息记忆。
      - 开（#56）：同一 conv 复用 session（serve 自带多轮历史）。命中未过期 sid 直接复用；
        POST 遇 404（session 被 serve 清了/重启失效）→ 删记录、重建一次重试。会话闲置 TTL
        过期或 LRU 逐出时删远端 session。同一 conv 串行（_conv_lock），不同 conv 并行。

    两种模式都把 sid（连同来源 conv ctx）登记到 core.brain 注册表，供 text_reply 抑制 SSE
    业务通知 + question/permission 把提问/审批路由回来源群。若该轮 agent 调 question/permission
    工具，POST 阻塞到用户答复（另一线程 POST reply 解阻塞），故 timeout 需覆盖等待时间。

    Returns: 回复文本（可能空串）；serve 不可用/出错返回 None（交给调用方回退 CLI）。
    """
    pid, port, pwd = find_serve_credentials()
    if not port:
        return None  # serve 没起或凭据缺失 → 回退
    conv_id = (ctx or {}).get("conv_id", "")
    if _SESSION_REUSE and conv_id:
        return _http_reuse(port, pwd, conv_id, prompt, ctx)
    return _http_oneshot(port, pwd, prompt, ctx)


def _http_oneshot(port, pwd, prompt, ctx):
    """旧语义：建 → 发 → 删，无状态。"""
    provider, model_id = _split_model(_OPENCODE_MODEL)
    t0 = time.time()
    sid = None
    try:
        sid = _create_session(port, pwd)
        _register_textreply_sid(sid, ctx)
        reply = _post_message(port, pwd, sid, prompt, provider, model_id)
        _oc_log("http", _OPENCODE_MODEL, time.time() - t0, prompt, reply, True)
        return reply
    except Exception as e:
        _oc_log("http", _OPENCODE_MODEL, time.time() - t0, prompt, "", False, str(e))
        log(f"brain opencode http err: {e}")
        return None  # 交给调用方回退 CLI
    finally:
        _delete_session(port, pwd, sid)


def _http_reuse(port, pwd, conv_id, prompt, ctx):
    """复用语义：同一 conv 串行走同一 session；404 失效则重建一次重试。"""
    provider, model_id = _split_model(_OPENCODE_MODEL)
    t0 = time.time()
    with _conv_lock(conv_id):
        sid = _lookup_sid(conv_id)
        reused = sid is not None
        try:
            if sid is None:
                sid = _create_session(port, pwd)
            _register_textreply_sid(sid, ctx)   # 刷新 conv ctx（回程路由用最新来源）
            try:
                reply = _post_message(port, pwd, sid, prompt, provider, model_id)
            except urllib.error.HTTPError as he:
                # 复用的 session 已被 serve 清（重启/GC）→ 丢记录、重建一次重试
                if reused and he.code == 404:
                    log(f"brain: 复用 session {sid[:12]} 失效(404)，重建 conv={conv_id[:12]}")
                    _forget_sid(conv_id)
                    sid = _create_session(port, pwd)
                    _register_textreply_sid(sid, ctx)
                    reply = _post_message(port, pwd, sid, prompt, provider, model_id)
                else:
                    raise
            # 成功：登记/刷新 last，处理 LRU 逐出（删被挤掉会话的远端 session）
            for _cid, _sid in _remember_sid(conv_id, sid):
                _delete_session(port, pwd, _sid)
            _oc_log("http", _OPENCODE_MODEL, time.time() - t0, prompt, reply, True)
            return reply
        except Exception as e:
            # 失败别把坏 sid 留在表里，避免后续消息一直命中坏会话
            _forget_sid(conv_id)
            _delete_session(port, pwd, sid)
            _oc_log("http", _OPENCODE_MODEL, time.time() - t0, prompt, "", False, str(e))
            log(f"brain opencode http err: {e}")
            return None  # 交给调用方回退 CLI


def _brain_opencode_cli(prompt):
    """回退路径：调 `opencode run <prompt> --model M --format json`，拼接 text 事件为回复。

    HTTP 不可用时的兜底。输出是 NDJSON 事件流，逐行取 type==text 的 part.text 拼接。
    """
    full_prompt = f"{_SYSTEM_PROMPT}\n\n{prompt}"
    cmd = [_OPENCODE_BIN, "run", full_prompt,
           "--model", _OPENCODE_MODEL, "--format", "json"]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=_OPENCODE_TIMEOUT)
    if r.returncode != 0:
        _oc_log("cli", _OPENCODE_MODEL, time.time() - t0, prompt, "", False,
                r.stderr[:200])
        log(f"opencode run rc={r.returncode} stderr={r.stderr[:200]}")
        return ""
    parts = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        if evt.get("type") == "text":
            parts.append(evt.get("part", {}).get("text", ""))
    reply = "".join(parts).strip()
    _oc_log("cli", _OPENCODE_MODEL, time.time() - t0, prompt, reply, True)
    return reply


# ---------------------------------------------------------------------------
# proxy 后端 — 经 LLM /chat/completions
# ---------------------------------------------------------------------------

def _brain_proxy(user, text, ctx, raw=False):
    """调用 LLM 生成回复（OpenAI 兼容 /chat/completions）。"""
    user_content = text if raw else f"{user}：{text}"
    body = json.dumps({
        "model": _CHAT_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }).encode()
    req = urllib.request.Request(
        f"{PROXY_URL}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {PROXY_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    r = urllib.request.urlopen(req, timeout=60)
    d = json.loads(r.read().decode("utf-8"))
    return (d.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()


# 把 opencode/proxy/echo 生成实现注册给 core.brain，让能力经 core.brain.generate_reply 统一调用。
from core.brain import register_brain  # noqa: E402
register_brain(generate_reply)
