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

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from core.agent_common import PROXY_URL, PROXY_KEY, find_serve_credentials, log, serve_request
from core.brain import STATUS_OK, STATUS_EMPTY, STATUS_FAILED

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

# 会话统计摘要（#63）：session 结束时发送统计信息。
#   AGENT_SESSION_SUMMARY_ENABLED   是否启用统计摘要（默认开启）。
#   AGENT_SESSION_SUMMARY_TRIGGERS  触发场景：reset(重置),ttl(过期),lru(逐出),command(/stats命令)，逗号分隔。
#   AGENT_SESSION_SUMMARY_O2O_ONLY  是否仅在单聊发送（默认 1，群聊不发避免噪音）。
_SUMMARY_ENABLED = os.environ.get("AGENT_SESSION_SUMMARY_ENABLED", "1") in ("1", "true", "True", "yes", "on")
_SUMMARY_TRIGGERS = {
    t.strip().lower()
    for t in os.environ.get("AGENT_SESSION_SUMMARY_TRIGGERS", "reset,command").split(",")
    if t.strip()
}
_SUMMARY_O2O_ONLY = os.environ.get("AGENT_SESSION_SUMMARY_O2O_ONLY", "1") in ("1", "true", "True", "yes", "on")


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
# 系统提示词（proxy/opencode 后端），可用环境变量覆盖。
# 点明数字员工身份 + 钉钉协同场景 + 多模态/多人对话能力，让 agent 更好理解工作语境。
_SYSTEM_PROMPT = os.environ.get(
    "AGENT_SYSTEM_PROMPT",
    "你是一个钉钉数字员工 Agent，通过钉钉群聊/私聊与用户协同工作。\n"
    "你需要理解多人对话场景（群聊中不同角色的发言、转发的聊天记录），识别任务意图并给出有帮助的回应。\n"
    "用户可能会发送文档、图片、文件、链接，系统已为你识别/转写这些内容并内联在消息里。\n"
    "回答简洁、准确、专业，用中文。当用户需要总结或归纳时，关注关键信息和行动项。",
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
# 统计扩展（#63）：增加 created/rounds/input_tokens/output_tokens/reasoning_tokens/cache_read/cache_write 字段。
from collections import OrderedDict                       # noqa: E402

_conv_sessions = OrderedDict()   # conv_id -> {
                                 #   "sid": str,
                                 #   "last": float,
                                 #   "created": float,           # 创建时间
                                 #   "rounds": int,              # 对话轮数
                                 #   "input_tokens": int,        # 输入 tokens
                                 #   "output_tokens": int,       # 输出 tokens
                                 #   "reasoning_tokens": int,    # 推理 tokens
                                 #   "cache_read": int,          # 缓存读取 tokens
                                 #   "cache_write": int,         # 缓存写入 tokens
                                 # }
_conv_locks = {}                 # conv_id -> threading.Lock（保护单会话内的顺序）
_conv_meta_lock = threading.Lock()   # 保护上面两张表本身的结构性改动


def _conv_lock(conv_id):
    """取某会话的串行锁（不存在则建）。"""
    with _conv_meta_lock:
        lk = _conv_locks.get(conv_id)
        if lk is None:
            lk = _conv_locks[conv_id] = threading.Lock()
        return lk


def _lookup_sid(conv_id, ctx=None):
    """查该 conv 未过期的 sid；过期/无则返回 None（过期项顺手删除）。

    ctx: 可选上下文 dict（含 conv_type），用于过期时发送统计摘要。
    """
    if not conv_id:
        return None
    with _conv_meta_lock:
        rec = _conv_sessions.get(conv_id)
        if not rec:
            return None
        if time.time() - rec["last"] > _SESSION_TTL:
            # TTL 过期：发送统计摘要（如果启用且提供了 ctx）
            if ctx:
                conv_type = ctx.get("conv_type", 1)
                # 在锁外发送（避免死锁）
                sid_to_send = rec["sid"]
                conv_id_to_send = conv_id
                _conv_sessions.pop(conv_id, None)   # 过期 → 丢弃，调用方重建
                # 释放锁后发送
                try:
                    if _should_send_summary(conv_id_to_send, conv_type, "ttl"):
                        stats = {
                            "sid": sid_to_send,
                            "created": rec.get("created", rec["last"]),
                            "elapsed": time.time() - rec.get("created", rec["last"]),
                            "rounds": rec.get("rounds", 0),
                            "input_tokens": rec.get("input_tokens", 0),
                            "output_tokens": rec.get("output_tokens", 0),
                            "reasoning_tokens": rec.get("reasoning_tokens", 0),
                            "cache_read": rec.get("cache_read", 0),
                            "cache_write": rec.get("cache_write", 0),
                            "model": _OPENCODE_MODEL,
                        }
                        summary = _format_session_summary(stats)
                        if summary:
                            from core.replier import send_reply
                            send_reply(conv_id_to_send, conv_type, summary)
                            log(f"brain: 已发送统计摘要 conv={conv_id_to_send[:12]} trigger=ttl")
                except Exception as e:
                    log(f"brain: TTL 过期发送统计摘要失败: {e}")
            else:
                _conv_sessions.pop(conv_id, None)   # 过期 → 丢弃，调用方重建
            return None
        _conv_sessions.move_to_end(conv_id)     # LRU：命中移到末尾
        return rec["sid"]


def _remember_sid(conv_id, sid, is_new=False):
    """登记/刷新 conv→sid，并做 LRU 逐出。返回被逐出的 (conv_id, sid) 列表供删远端 session。

    is_new=True 时初始化统计字段；False 时只刷新 last。
    """
    evicted = []
    if not conv_id or not sid:
        return evicted
    with _conv_meta_lock:
        if is_new or conv_id not in _conv_sessions:
            _conv_sessions[conv_id] = {
                "sid": sid,
                "last": time.time(),
                "created": time.time(),
                "rounds": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cache_read": 0,
                "cache_write": 0,
            }
        else:
            _conv_sessions[conv_id]["sid"] = sid
            _conv_sessions[conv_id]["last"] = time.time()
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


def _update_stats(conv_id, input_tokens=0, output_tokens=0, reasoning_tokens=0, cache_read=0, cache_write=0):
    """更新会话统计信息（轮数 + tokens）。"""
    if not conv_id:
        return
    with _conv_meta_lock:
        rec = _conv_sessions.get(conv_id)
        if rec:
            rec["rounds"] += 1
            rec["input_tokens"] += input_tokens
            rec["output_tokens"] += output_tokens
            rec["reasoning_tokens"] += reasoning_tokens
            rec["cache_read"] += cache_read
            rec["cache_write"] += cache_write
            rec["last"] = time.time()


def _get_session_stats(conv_id):
    """获取会话统计信息。返回 dict 或 None。"""
    if not conv_id:
        return None
    with _conv_meta_lock:
        rec = _conv_sessions.get(conv_id)
        if not rec:
            return None
        return {
            "sid": rec["sid"],
            "created": rec.get("created", rec["last"]),
            "elapsed": time.time() - rec.get("created", rec["last"]),
            "rounds": rec.get("rounds", 0),
            "input_tokens": rec.get("input_tokens", 0),
            "output_tokens": rec.get("output_tokens", 0),
            "reasoning_tokens": rec.get("reasoning_tokens", 0),
            "cache_read": rec.get("cache_read", 0),
            "cache_write": rec.get("cache_write", 0),
            "model": _OPENCODE_MODEL,
        }


def _format_tokens(count):
    """格式化 token 数量（K/M 单位）。"""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1000:
        return f"{count / 1000:.1f}K"
    return str(count)


def _format_session_summary(stats):
    """格式化会话统计摘要消息。"""
    if not stats:
        return None

    sid = stats.get("sid", "unknown")[:12]
    elapsed = int(stats.get("elapsed", 0))
    model = stats.get("model", "unknown")
    rounds = stats.get("rounds", 0)
    input_tokens = stats.get("input_tokens", 0)
    output_tokens = stats.get("output_tokens", 0)
    reasoning_tokens = stats.get("reasoning_tokens", 0)
    cache_read = stats.get("cache_read", 0)
    cache_write = stats.get("cache_write", 0)

    # 基础信息（总是显示）
    lines = [
        f"**Session ID:** `{sid}`",
        "",
    ]

    # 使用 markdown 列表格式，每个字段一行
    lines.append(f"- ⏱️ **耗时:** {elapsed}s")
    lines.append(f"- 🤖 **模型:** {model}")
    lines.append(f"- 🔄 **轮数:** {rounds}")

    # Tokens 统计（总是显示，即使是 0）
    input_str = _format_tokens(input_tokens)
    output_str = _format_tokens(output_tokens)
    lines.append(f"- 💬 **Tokens:** 输入 {input_str}↑ / 输出 {output_str}↓")

    # 推理 tokens（只在 > 0 时显示）
    if reasoning_tokens > 0:
        reasoning_str = _format_tokens(reasoning_tokens)
        lines.append(f"- 🧠 **推理:** {reasoning_str}")

    # 缓存命中率（只在有缓存读取时显示）
    if cache_read > 0:
        total_in = input_tokens + cache_read
        hit_rate = (cache_read / total_in * 100) if total_in > 0 else 0
        cache_read_str = _format_tokens(cache_read)
        total_in_str = _format_tokens(total_in)
        lines.append(f"- 🔄 **缓存命中:** {hit_rate:.1f}%（{cache_read_str}/{total_in_str}）")

    # 窗口使用率（只在有输入时显示，窗口占用 = 新输入 + 缓存命中）
    ctx_used = input_tokens + cache_read
    if ctx_used > 0:
        window_size = 1_000_000
        window_pct = (ctx_used / window_size * 100) if window_size > 0 else 0
        ctx_used_str = _format_tokens(ctx_used)
        window_size_str = _format_tokens(window_size)
        lines.append(f"- 📊 **窗口:** {ctx_used_str}/{window_size_str}（{window_pct:.1f}%）")

    return "\n".join(lines)


def _should_send_summary(conv_id, conv_type, trigger):
    """判断是否应该发送统计摘要。

    Args:
        conv_id: 会话 ID
        conv_type: 会话类型（1=单聊，2=群聊）
        trigger: 触发场景（reset/ttl/lru/command）

    Returns:
        bool: 是否发送
    """
    if not _SUMMARY_ENABLED:
        return False
    if trigger not in _SUMMARY_TRIGGERS:
        return False
    if _SUMMARY_O2O_ONLY and conv_type != 1:
        return False
    return True


def _send_session_summary(conv_id, conv_type, trigger="reset"):
    """发送会话统计摘要。

    Args:
        conv_id: 会话 ID
        conv_type: 会话类型
        trigger: 触发场景（reset/ttl/lru/command）
    """
    if not _should_send_summary(conv_id, conv_type, trigger):
        return

    stats = _get_session_stats(conv_id)
    if not stats:
        return

    summary = _format_session_summary(stats)
    if not summary:
        return

    # 延迟导入避免循环依赖
    try:
        from core.replier import send_reply
        send_reply(conv_id, conv_type, summary)
        log(f"brain: 已发送统计摘要 conv={conv_id[:12]} trigger={trigger}")
    except Exception as e:
        log(f"brain: 发送统计摘要失败: {e}")


def _reset_sessions():
    """清空复用表（测试用）。"""
    with _conv_meta_lock:
        _conv_sessions.clear()
        _conv_locks.clear()



def generate_reply(user, text, ctx=None, raw=False):
    """根据用户消息生成回复文本。返回空串 = 不回复（向后兼容的纯字符串契约）。"""
    reply, _status = generate_reply_ex(user, text, ctx=ctx, raw=raw)
    return reply


def generate_reply_ex(user, text, ctx=None, raw=False):
    """生成回复 + 状态（#59）。返回 (reply, status)，status ∈ ok/empty/failed。

    Args:
        user: 发送者展示名
        text: 消息正文（已 strip）
        ctx:  可选上下文 dict（conv_id / msg_id / conv_type 等）
        raw:  True 时 text 已是**完整 prompt**，后端不再拼 "{user}：" 前缀
              （合并转发等已自行组装结构化 prompt 的调用方用它，避免前缀污染上下文）

    失败语义：opencode 后端 serve HTTP 与 CLI 回退都不可用/超时/异常 → failed（让上层
    发兜底提示 + ack 落失败终态）。echo/proxy 后端抛异常 → failed；正常返回空 → empty。
    """
    text = (text or "").strip()
    if not text:
        return "", STATUS_EMPTY
    try:
        if _BRAIN == "proxy":
            reply, status = _brain_proxy(user, text, ctx, raw=raw), None
        elif _BRAIN == "opencode":
            reply, status = _brain_opencode(user, text, ctx, raw=raw)
        else:
            reply, status = _brain_echo(user, text, ctx), None
    except Exception as e:
        log(f"brain({_BRAIN}) err: {e}")
        return "", STATUS_FAILED
    if reply and len(reply) > _MAX_REPLY_CHARS:
        reply = reply[:_MAX_REPLY_CHARS] + "…（已截断）"
    if status is None:
        status = STATUS_OK if reply else STATUS_EMPTY
    return reply, status


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
    返回 (reply, status)，status ∈ ok/empty/failed（#59）。

    HTTP 复用常驻 serve 进程，省掉每次 CLI 冷启动（实测 ~3x）；serve 未起/凭据缺失/
    请求异常时无缝回退到一次性子进程。两条路都不可用/超时/出错 → failed（上层发兜底
    提示 + ack 落失败终态），而非静默吞消息。

    会话连续性（#56）：开启 AGENT_SESSION_REUSE 时，同一 conv 复用 serve session 带多轮
    上下文；用户发重置关键词（/new 等）→ 断上下文重建，不打扰模型直接回确认。
    """
    conv_id = (ctx or {}).get("conv_id", "")
    conv_type = (ctx or {}).get("conv_type", 1)
    # 重置指令：仅在复用模式下有意义（无状态模式每条本就是新会话）
    if _SESSION_REUSE and conv_id and _is_reset(text):
        # 发送统计摘要（如果启用）
        _send_session_summary(conv_id, conv_type, trigger="reset")
        old = _forget_sid(conv_id)
        if old:
            pid, port, pwd = find_serve_credentials()
            if port:
                _delete_session(port, pwd, old)
        return "🆕 已开启新话题，之前的上下文已清空。", STATUS_OK

    prompt = text if raw else f"{user}：{text}"
    reply = _brain_opencode_http(prompt, ctx=ctx)
    if reply is not None:
        # HTTP 后端正常应答（可能空）：非空=ok，空=模型未产出=empty
        return reply, (STATUS_OK if reply else STATUS_EMPTY)
    # HTTP 不可用（serve 没起/凭据缺失/异常）→ 回退 CLI（无状态，拿不到 serve session）
    log("brain(opencode): serve HTTP 不可用，回退 opencode run CLI")
    try:
        cli_reply = _brain_opencode_cli(prompt)
    except Exception as e:
        # CLI 也挂了（超时 / rc!=0 / opencode 不存在）→ 彻底失败，给用户兜底
        log(f"brain(opencode): CLI 回退失败：{e}")
        return "", STATUS_FAILED
    return cli_reply, (STATUS_OK if cli_reply else STATUS_EMPTY)


def _serve_request(method, port, pwd, path, body=None, timeout=8):
    """向 opencode serve 发一个 HTTP 请求（薄适配器）。

    实现已统一到 core.agent_common.serve_request（凭据/Basic auth/调试日志集中一处）；
    这里保留旧的位置参数签名，让内部调用点与测试桩（patch brain._serve_request）不变。
    调试日志由 AGENT_SERVE_DEBUG 开关控制，见 serve_request。
    """
    return serve_request(method, path, body, timeout, port=port, pwd=pwd)


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
    """向 session 发一条 message，拼接 text parts 返回回复文本和统计信息。

    返回 (reply_text, usage_dict)，usage_dict 包含 input/output/reasoning/cache tokens。
    """
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

    # 提取 token 使用统计（支持多种格式）
    # 格式1: info.tokens (opencode serve 实际格式)
    info = d.get("info", {}) or {}
    info_tokens = info.get("tokens", {}) or {}

    # 格式2: tokens (参考项目格式，可能用于 SSE 事件)
    tokens = d.get("tokens", {}) or {}

    # 格式3: usage (驼峰命名，备用)
    usage = d.get("usage", {}) or {}

    # 优先使用 info.tokens（实际响应格式），然后 fallback
    cache = info_tokens.get("cache") or tokens.get("cache", {}) or {}
    return reply, {
        "input_tokens": info_tokens.get("input") or tokens.get("input") or usage.get("inputTokens", 0),
        "output_tokens": info_tokens.get("output") or tokens.get("output") or usage.get("outputTokens", 0),
        "reasoning_tokens": info_tokens.get("reasoning") or tokens.get("reasoning") or usage.get("reasoningTokens", 0),
        "cache_read": cache.get("read") or usage.get("cacheReadTokens", 0),
        "cache_write": cache.get("write") or usage.get("cacheWriteTokens", 0),
    }


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
        reply, usage = _post_message(port, pwd, sid, prompt, provider, model_id)
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
        sid = _lookup_sid(conv_id, ctx=ctx)
        reused = sid is not None
        try:
            if sid is None:
                sid = _create_session(port, pwd)
            _register_textreply_sid(sid, ctx)   # 刷新 conv ctx（回程路由用最新来源）
            try:
                reply, usage = _post_message(port, pwd, sid, prompt, provider, model_id)
            except urllib.error.HTTPError as he:
                # 复用的 session 已被 serve 清（重启/GC）→ 丢记录、重建一次重试
                if reused and he.code == 404:
                    log(f"brain: 复用 session {sid[:12]} 失效(404)，重建 conv={conv_id[:12]}")
                    _forget_sid(conv_id)
                    sid = _create_session(port, pwd)
                    _register_textreply_sid(sid, ctx)
                    reply, usage = _post_message(port, pwd, sid, prompt, provider, model_id)
                    reused = False  # 重建了，视为新会话
                else:
                    raise
            # 成功：登记/刷新 last，处理 LRU 逐出（删被挤掉会话的远端 session）
            for _cid, _sid in _remember_sid(conv_id, sid, is_new=(not reused)):
                _delete_session(port, pwd, _sid)
            # 更新统计信息
            _update_stats(conv_id,
                         input_tokens=usage.get("input_tokens", 0),
                         output_tokens=usage.get("output_tokens", 0),
                         reasoning_tokens=usage.get("reasoning_tokens", 0),
                         cache_read=usage.get("cache_read", 0),
                         cache_write=usage.get("cache_write", 0))
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
    失败（超时 / rc!=0 / opencode 不存在）**抛异常**，由调用方判为 failed（#59）——不再
    把失败伪装成空字符串，以便上层给用户兜底提示。
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
        raise RuntimeError(f"opencode run rc={r.returncode}")
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
# 把 opencode/proxy/echo 生成实现注册给 core.brain，让能力经 core.brain.generate_reply 统一调用。
# 同时注册状态感知实现（#59）：text_reply 经 generate_reply_ex 拿 ok/empty/failed 区分。
from core.brain import register_brain, register_brain_ex  # noqa: E402
register_brain(generate_reply)
register_brain_ex(generate_reply_ex)
