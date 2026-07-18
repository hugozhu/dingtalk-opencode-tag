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

接口：generate_reply(user, text, ctx=None) -> str（返回空串表示不回复）
"""

import base64
import json
import os
import subprocess
import threading
import time
import urllib.request
from collections import OrderedDict

from core.agent_common import PROXY_URL, PROXY_KEY, find_serve_credentials, log

# 大脑后端选择
_BRAIN = os.environ.get("AGENT_BRAIN", "echo")
# proxy 后端用的对话模型（区别于 VISION_MODEL）
_CHAT_MODEL = os.environ.get("AGENT_CHAT_MODEL", "gpt-4o-mini")
# opencode 后端用的模型（provider/model 格式；免鉴权可用 *-free）
_OPENCODE_MODEL = os.environ.get("AGENT_OPENCODE_MODEL", "opencode/deepseek-v4-flash-free")
_OPENCODE_BIN = os.environ.get("AGENT_OPENCODE_BIN", "opencode")
_OPENCODE_TIMEOUT = int(os.environ.get("AGENT_OPENCODE_TIMEOUT", "90"))

# 调试开关 + opencode 调用独立日志（与 agent_common 的 AGENT_DEBUG 语义一致）
_DEBUG = os.environ.get("AGENT_DEBUG", "") in ("1", "true", "True")
_PROJECT_ROOT = os.environ.get(
    "PROJECT_DIR",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_OPENCODE_LOG = os.environ.get(
    "AGENT_OPENCODE_LOG", os.path.join(_PROJECT_ROOT, "opencode.log"))

# brain 文本回复的临时 session id 登记表（有界 FIFO）。
# brain 现在与 event_watcher 的 SSE 循环同进程共享内存：brain 在托管 serve 上建的
# 临时 session 会在 SSE 流里冒出 session.status/idle 事件，若不区分会触发 core
# format_and_forward 的"收到新请求/会话完成"业务通知（刷屏）。这里登记 brain 自己的
# session id（连同来源会话 conv 上下文），供：
#   - custom.routes.route_sse_event 抑制这些事件（不影响合并转发业务 session）；
#   - question 能力把 question.asked / 答案路由回**来源群**（事件只有 sessionID，无 conv_id）。
# 值是 ctx dict（含 conv_id/conv_type），无 ctx 时为 {}。有界 + FIFO。
_TEXTREPLY_SIDS = OrderedDict()
_TEXTREPLY_SIDS_MAX = 256
_textreply_lock = threading.Lock()


def _register_textreply_sid(sid, ctx=None):
    """登记 brain 临时 session；ctx 可含来源会话（conv_id/conv_type）供事件回程路由。"""
    if not sid:
        return
    with _textreply_lock:
        _TEXTREPLY_SIDS[sid] = dict(ctx or {})
        while len(_TEXTREPLY_SIDS) > _TEXTREPLY_SIDS_MAX:
            _TEXTREPLY_SIDS.popitem(last=False)


def is_textreply_session(sid):
    """判断某 SSE sessionID 是否是 brain 文本回复的临时 session。

    供 custom.routes.route_sse_event 调用：命中则抑制该事件的业务通知。
    """
    if not sid:
        return False
    with _textreply_lock:
        return sid in _TEXTREPLY_SIDS


def session_conv(sid):
    """取某 session 登记的来源会话 ctx（{conv_id, conv_type, ...}）；未登记返回 None。

    question 能力用它把 question.asked / 答案路由回来源群。
    """
    if not sid:
        return None
    with _textreply_lock:
        v = _TEXTREPLY_SIDS.get(sid)
        return dict(v) if v is not None else None
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
    """
    prompt = text if raw else f"{user}：{text}"
    reply = _brain_opencode_http(prompt, ctx=ctx)
    if reply is not None:
        return reply
    # HTTP 不可用（serve 没起/凭据缺失/异常）→ 回退 CLI
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


def _brain_opencode_http(prompt, ctx=None):
    """走 opencode serve HTTP 生成回复。

    流程：发现 serve 凭据 → 建临时 session → POST message（带 system + model）→
    拼 text parts → best-effort 删 session（保持与 CLI 一样的无状态语义，避免 session 堆积）。

    ctx（含 conv_id/conv_type）登记到 session 注册表，供 question 能力把 agent 提问/答案
    路由回来源群。若该轮 agent 调 question 工具，POST 会阻塞到用户答复（另一线程 POST
    reply 解阻塞），故 timeout 需覆盖等待答案的时间。

    Returns: 回复文本（可能空串）；serve 不可用/出错返回 None（交给调用方回退 CLI）。
    """
    pid, port, pwd = find_serve_credentials()
    if not port:
        return None  # serve 没起或凭据缺失 → 回退
    provider, model_id = _split_model(_OPENCODE_MODEL)
    t0 = time.time()
    sid = None
    try:
        created = _serve_request("POST", port, pwd, "/session",
                                 {"title": "agent-textreply"}, timeout=10)
        sid = (created or {}).get("id")
        if not sid:
            raise RuntimeError("create session 无 id")
        # 登记为 brain 临时 session（带来源会话 ctx）：抑制 SSE 业务通知 + question 回程路由
        _register_textreply_sid(sid, ctx)
        d = _serve_request(
            "POST", port, pwd, f"/session/{sid}/message",
            {
                "model": {"providerID": provider, "modelID": model_id},
                "system": _SYSTEM_PROMPT,
                "parts": [{"type": "text", "text": prompt}],
            },
            timeout=_OPENCODE_TIMEOUT,
        ) or {}
        reply = "".join(
            p.get("text", "") for p in d.get("parts", []) if p.get("type") == "text"
        ).strip()
        _oc_log("http", _OPENCODE_MODEL, time.time() - t0, prompt, reply, True)
        return reply
    except Exception as e:
        _oc_log("http", _OPENCODE_MODEL, time.time() - t0, prompt, "", False, str(e))
        log(f"brain opencode http err: {e}")
        return None  # 交给调用方回退 CLI
    finally:
        if sid:
            try:
                _serve_request("DELETE", port, pwd, f"/session/{sid}", timeout=6)
            except Exception:
                pass  # session 清理失败不影响回复


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
