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
# 便宜模型逐轮切换（#117）：消息带触发词 → 本轮改用 FLASH 模型。模型随每条 message POST
# 传（见 _post_message），但**命中的这一轮不复用主会话**：provider 的 prompt cache 是按
# 模型分桶的，在攒了长上下文的复用 session 里换模型 = 整段历史重新编码，比省下的还贵
# （见 _brain_opencode_http 的实测数字）。故这一轮单独建一次性 session 跑完即删，代价是
# 看不到前文。留空=特性关闭。
# 触发词是**子串**匹配（修饰语跟在真实任务前），区别于 CANCEL/RESET 的整句严格匹配。
_OPENCODE_MODEL_FLASH = os.environ.get("AGENT_OPENCODE_MODEL_FLASH", "")
# 长的排前面：「用flash」是「用flash模型」的前缀，短的先匹配会在文本里留下孤零零的「模型」。
_FLASH_KEYWORDS = sorted(
    (k.strip().lower()
     for k in os.environ.get("AGENT_OPENCODE_FLASH_KEYWORDS",
                             "用flash模型,use flash model,用flash,/flash").split(",")
     if k.strip()),
    key=len, reverse=True,
)
_OPENCODE_BIN = os.environ.get("AGENT_OPENCODE_BIN", "opencode")
# 长程任务超时（#75）：活动感知，不再一刀切墙钟硬超时。serve 的 message POST 是同步阻塞
# 请求，任务跑完才返回，其间 socket 无字节流动，旧的 urllib socket 超时 = 任务总时长上限，
# 长任务超 5min 即被强杀。改为：socket 超时放大到 MAX 兜底 + watchdog 轮询 session 活动，
# 只要仍在产出就不 abort（见 _start_watchdog）。
#   AGENT_OPENCODE_IDLE_TIMEOUT  无活动多少秒判定卡死并 abort（默认 300）
#   AGENT_OPENCODE_MAX_TIMEOUT   绝对上限硬超时兜底（默认 3600；0=不设上限，socket timeout=None）
#   AGENT_OPENCODE_ACTIVITY_POLL 活动探测间隔秒（默认 15）
# 兼容：显式设了旧 AGENT_OPENCODE_TIMEOUT 时，作为 IDLE_TIMEOUT 的默认值，老配置无缝迁移。
_LEGACY_TIMEOUT = os.environ.get("AGENT_OPENCODE_TIMEOUT")
_OPENCODE_IDLE_TIMEOUT = int(os.environ.get("AGENT_OPENCODE_IDLE_TIMEOUT", _LEGACY_TIMEOUT or "300"))
_OPENCODE_MAX_TIMEOUT = int(os.environ.get("AGENT_OPENCODE_MAX_TIMEOUT", "3600"))
_OPENCODE_ACTIVITY_POLL = int(os.environ.get("AGENT_OPENCODE_ACTIVITY_POLL", "15"))
_OPENCODE_SOCK_TIMEOUT = _OPENCODE_MAX_TIMEOUT or None   # 0 → None（阻塞到 watchdog/abort）
# CLI 回退硬超时（serve 挂时的兜底路径，长程主要走 HTTP）
_OPENCODE_TIMEOUT = _OPENCODE_MAX_TIMEOUT or None
# 会话中毒自愈（后端 stream-error 会让复用 session 回空/终态坏回合，卡住后续所有轮）：
#   AGENT_OPENCODE_EMPTY_RETRY  复用会话回空 → 丢弃 sid 并新建重试一次（默认开）
#   AGENT_OPENCODE_ERROR_ABORT  助手消息 completed-with-error/空 → watchdog 立即 abort，不等 idle（默认开）
_OPENCODE_EMPTY_RETRY = os.environ.get("AGENT_OPENCODE_EMPTY_RETRY", "1") in ("1", "true", "yes", "on")
_OPENCODE_ERROR_ABORT = os.environ.get("AGENT_OPENCODE_ERROR_ABORT", "1") in ("1", "true", "yes", "on")

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
# 回复总长度上限（外层 backstop，防跑飞刷屏）。超长回复由 replier 按此上限内的内容
# 分片成多条钉钉消息发出（见 custom/replier.py _split_text），不再一刀切到千字。
_MAX_REPLY_CHARS = int(os.environ.get("AGENT_MAX_REPLY_CHARS", "12000"))


def _oc_log(transport, model, elapsed, prompt, reply, ok, err="", sess=""):
    """把一次 opencode 调用记到独立 opencode.log。

    成功记录仅在 AGENT_DEBUG=1 时写；失败（ok=False）恒记，不受开关影响，
    便于事后排查"回复为空/超时"到底断在 HTTP 还是 CLI。best-effort，写失败静默。

    sess: 会话语义标记（reuse=复用主会话 / oneshot=一次性会话，含逐轮模型分流的轮次）。
    **只能追加在 transport= 之后**：healthcheck 的熔断正则锚行首并限定 transport 取值
    （bin/core/healthcheck.sh 的 _BRAIN_FAIL_PATTERN），插到前面会让计数器失效。
    """
    if ok and not _DEBUG:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    preview = (reply or "").replace("\n", " ")[:80]
    line = (f"[{ts}] transport={transport} model={model} "
            f"elapsed={elapsed:.2f}s prompt_len={len(prompt or '')} "
            f"reply_len={len(reply or '')} ok={ok}")
    if sess:
        line += f" sess={sess}"
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
                            "flash_rounds": rec.get("flash_rounds", 0),
                            "flash_input_tokens": rec.get("flash_input_tokens", 0),
                            "flash_output_tokens": rec.get("flash_output_tokens", 0),
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
                # 逐轮模型（#117 跟进）分流到独立一次性 session 的轮次：单独计，不并进上面
                # 的主计数器（那些数用来算本会话的窗口占用/缓存命中，flash 轮没进过主会话）
                "flash_rounds": 0,
                "flash_input_tokens": 0,
                "flash_output_tokens": 0,
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


def _find_flash_trigger(low):
    """在 low（已 lower）里找触发词，返回 (start, keyword) 或 None。

    带 ASCII 词边界：触发词以字母/数字收尾时，后面不能紧跟字母/数字，否则
    「/flashlight」「useflashmodel」这种会被误判成触发词、还把 prompt 割坏。
    中文触发词（如「用flash模型」以「型」收尾）不受此限——中文不用空格分词。
    """
    for k in _FLASH_KEYWORDS:
        start = 0
        while True:
            i = low.find(k, start)
            if i < 0:
                break
            after = low[i + len(k):i + len(k) + 1]
            if not (k[-1].isascii() and k[-1].isalnum() and after.isalnum() and after.isascii()):
                return i, k
            start = i + 1
    return None


def _pick_model(text):
    """按本轮文本选模型（#117），返回 (model, cleaned_text)。

    命中触发词 → 用便宜的 FLASH 模型，并把触发词从文本里摘掉：触发词是给 harness 看的
    修饰语（「用flash模型 打开浏览器抓股价」），留在 prompt 里模型会当成一条它执行不了的
    指令，可能回「好的，我将使用 flash 模型」这种噪音。

    未配置 FLASH 模型 = 特性关闭：原样返回，**也不摘触发词**——否则关掉特性反而会让
    用户的原话被悄悄改写。
    """
    if not _OPENCODE_MODEL_FLASH or not text:
        return _OPENCODE_MODEL, text
    found = _find_flash_trigger(text.lower())
    if not found:
        return _OPENCODE_MODEL, text
    i, hit = found
    cleaned = (text[:i] + text[i + len(hit):]).strip()
    if not cleaned:
        # 整句只有触发词、没有任务：摘完就空了，发空 prompt 没意义。当普通消息处理。
        return _OPENCODE_MODEL, text
    return _OPENCODE_MODEL_FLASH, cleaned


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


def _update_flash_stats(conv_id, input_tokens=0, output_tokens=0):
    """把「逐轮模型独立 session」轮次的用量记到该 conv 名下（#117 跟进）。

    **单独计数、不并进主计数器**：这些 token 从没进过复用 session，并进去会把
    _format_session_summary 的窗口占用/缓存命中率算歪（flash 轮是全量重编码，
    一轮就能虚增几万 input），而这两个数正是用户判断该不该 /new 的依据。
    也**不刷新 last**——主会话本轮没被用到，TTL 该照常走。
    无记录时静默 no-op（无状态模式 / 主会话还没建）。
    """
    if not conv_id:
        return
    with _conv_meta_lock:
        rec = _conv_sessions.get(conv_id)
        if rec:
            rec["flash_rounds"] = rec.get("flash_rounds", 0) + 1
            rec["flash_input_tokens"] = rec.get("flash_input_tokens", 0) + (input_tokens or 0)
            rec["flash_output_tokens"] = rec.get("flash_output_tokens", 0) + (output_tokens or 0)


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
            "flash_rounds": rec.get("flash_rounds", 0),
            "flash_input_tokens": rec.get("flash_input_tokens", 0),
            "flash_output_tokens": rec.get("flash_output_tokens", 0),
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

    # 逐轮模型（#117 跟进）：这些轮跑在独立一次性 session，不占本会话窗口，单列一行——
    # 免得 /stats 把它们的花费漏掉，又不污染下面窗口/缓存命中的口径。
    flash_rounds = stats.get("flash_rounds", 0)
    if flash_rounds > 0:
        flash_in = _format_tokens(stats.get("flash_input_tokens", 0))
        flash_out = _format_tokens(stats.get("flash_output_tokens", 0))
        lines.append(f"- ⚡ **独立模型轮:** {flash_rounds} 轮"
                     f"（输入 {flash_in}↑ / 输出 {flash_out}↓，不占本会话窗口）")

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


# ---------------------------------------------------------------------------
# 单次任务统计（#76）：每次任务成功产出后暂存本次 delta，供 task_stats 能力在「回复已发出」
# 后推送（用 send_notice 不广播，避免误触发 ack 收尾，且顺序在回复之后）。与 #63 会话摘要
# （累计、会话结束触发）互补：这里是**单次交互**的耗时/规模/缓存命中。
# ---------------------------------------------------------------------------
_last_task_stats = {}                    # conv_id -> {"usage": dict, "elapsed": float}
_last_task_stats_lock = threading.Lock()
_TASK_STATS_MAX = 256


def _stash_task_stats(conv_id, usage, elapsed):
    """暂存某 conv 最近一次任务的统计（成功产出后调用）。有界，防泄漏。"""
    if not conv_id:
        return
    with _last_task_stats_lock:
        _last_task_stats[conv_id] = {"usage": dict(usage or {}), "elapsed": elapsed}
        while len(_last_task_stats) > _TASK_STATS_MAX:
            _last_task_stats.pop(next(iter(_last_task_stats)))


def pop_task_stats(conv_id):
    """取出并清除某 conv 最近一次任务统计（无则 None）。供 task_stats 能力调用。"""
    if not conv_id:
        return None
    with _last_task_stats_lock:
        return _last_task_stats.pop(conv_id, None)


def format_task_stats(rec):
    """格式化单次任务统计消息（本次 delta + 缓存命中率）。rec={"usage","elapsed"}。无有效数据返回 None。"""
    if not rec:
        return None
    usage = rec.get("usage", {}) or {}
    elapsed = rec.get("elapsed", 0) or 0
    it = usage.get("input_tokens", 0) or 0
    ot = usage.get("output_tokens", 0) or 0
    rt = usage.get("reasoning_tokens", 0) or 0
    cr = usage.get("cache_read", 0) or 0
    tool_calls = usage.get("tool_calls", 0) or 0

    lines = ["📊 **本次任务统计**", ""]
    lines.append(f"- ⏱️ **耗时:** {elapsed:.1f}s")
    if tool_calls > 0:
        lines.append(f"- 🔧 **工具调用:** {tool_calls}")
    lines.append(f"- 💬 **Tokens:** 输入 {_format_tokens(it)}↑ / 输出 {_format_tokens(ot)}↓")
    if rt > 0:
        lines.append(f"- 🧠 **推理:** {_format_tokens(rt)}")
    if cr > 0:
        total_in = it + cr
        hit_rate = (cr / total_in * 100) if total_in > 0 else 0
        lines.append(f"- 🔄 **缓存命中:** {hit_rate:.1f}%（{_format_tokens(cr)}/{_format_tokens(total_in)}）")
    return "\n".join(lines)


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

    model, text = _pick_model(text)
    prompt = text if raw else f"{user}：{text}"
    try:
        reply = _brain_opencode_http(prompt, ctx=ctx, model=model)
    except _IdleAbort as e:
        # watchdog 空闲/超上限主动 abort → 终态失败，**不回退 CLI**（避免整轮长任务被重复执行）
        log(f"brain(opencode): 活动感知超时 abort，判失败不回退 CLI：{e}")
        return "", STATUS_FAILED
    if reply is not None:
        # HTTP 后端正常应答（可能空）：非空=ok，空=模型未产出=empty
        return reply, (STATUS_OK if reply else STATUS_EMPTY)
    # HTTP 不可用（serve 没起/凭据缺失/异常）→ 回退 CLI（无状态，拿不到 serve session）
    log("brain(opencode): serve HTTP 不可用，回退 opencode run CLI")
    try:
        cli_reply = _brain_opencode_cli(prompt, model=model)
    except Exception as e:
        # CLI 也挂了（超时 / rc!=0 / opencode 不存在）→ 彻底失败，给用户兜底
        log(f"brain(opencode): CLI 回退失败：{e}")
        return "", STATUS_FAILED
    return cli_reply, (STATUS_OK if cli_reply else STATUS_EMPTY)


def _serve_request(method, port, pwd, path, body=None, timeout=8):
    """向 opencode serve 发一个 HTTP 请求（薄适配器）。

    实现已统一到 core.agent_common.serve_request（凭据/Basic auth/调试日志集中一处）；
    这里保留旧的位置参数签名，让内部调用点与测试桩（patch brain._serve_request）不变。
    调试日志由 AGENT_DEBUG 开关控制，见 serve_request。
    """
    return serve_request(method, path, body, timeout, port=port, pwd=pwd)


def _session_title(ctx=None, prompt="", default="agent-textreply"):
    """为 serve session 生成一个可辨识的 title，便于在 opencode 后台按名字定位（#89）。

    旧行为：所有 session 都叫 "agent-textreply"，后台列表长一个样、无法区分来源。
    现在带上会话来源标记 + 发送者 + 首条消息摘要，例如：
      "[群] 张三 · 帮我看下这个报错的原因"
      "[私] 李四 · image"

    无 ctx / 无摘要时回退到 default，保证行为向后兼容（e2e 直接调 _create_session(port,pwd) 不受影响）。
    title 只用于后台展示：单行、脱换行、限长，避免过长或泄露大段内容。
    """
    ctx = ctx or {}
    conv_type = str(ctx.get("conv_type", "")).strip()
    marker = {"1": "[私]", "2": "[群]"}.get(conv_type, "")
    user = str(ctx.get("user", "")).strip()
    # 摘要：折叠所有空白为单空格并截断；prompt 可能形如 "user：text"，去掉冗余的前缀。
    summary = " ".join((prompt or "").split())
    if user and summary.startswith(f"{user}："):
        summary = summary[len(user) + 1:].lstrip()
    if len(summary) > 24:
        summary = summary[:24] + "…"
    parts = [p for p in (marker, user) if p]
    head = " ".join(parts)
    if head and summary:
        return f"{head} · {summary}"
    return head or summary or default


def _create_session(port, pwd, title=None):
    """建一个 serve session（带可选 per-session 权限规则）。返回 sid 或抛错。

    title 缺省时回退到 "agent-textreply"（旧行为）；传入时用它做后台展示名（#89）。
    """
    body = {"title": title or "agent-textreply"}
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


class _IdleAbort(Exception):
    """watchdog 因空闲/超上限主动 abort 了会话 → 终态失败，**不回退 CLI**（避免整轮长任务被重复执行）。"""


class _ServeTurnError(Exception):
    """serve HTTP 200 但回合以错误收尾（info.error，如模型网关 APIError）且无文本产出。

    与 _IdleAbort 不同：这不是卡死 abort，而是后端把失败正常返回了。必须向上抛成
    failed（→ 兜底提示 + ack 落失败终态），**不能**伪装成空回复——否则用户收不到任何
    反馈，ack 一直「仍在处理中」心跳到超时。
    """


def _activity_fingerprint(port, pwd, sid):
    """探测 session 当前活动指纹；变化=agent 仍在产出。

    读 GET /session/{sid}/message，取最后一条（进行中的 assistant）消息的
    (消息数, part 数, 文本总长, 最后更新时刻)。任一维变化即视为有活动。
    GET 失败返回 None：watchdog 据此**不判卡死**（偏向保活，降低误杀）。

    返回 (fingerprint, reasoning_in_progress)。
    reasoning_in_progress=True 表示最后一条 assistant 消息里有一个 **还没结束的
    reasoning part**（type=reasoning 且 time 只有 start 无 end）。这类 part 在
    GET /message 里 text 恒为空、time.updated/completed 也不置位，纯靠指纹数值
    变化会把它误判成「无活动」——模型其实还在深度推理（qwen3-8-max 单段推理
    可长达数百秒）。watchdog 见到该标记即视为仍在产出，不空转 idle。
    """
    try:
        msgs = _serve_request("GET", port, pwd, f"/session/{sid}/message", timeout=6)
    except Exception:
        return None
    if not isinstance(msgs, list) or not msgs:
        return (0, 0, 0, 0), False
    last = msgs[-1] or {}
    info = last.get("info", {}) or {}
    t = info.get("time", {}) or {}
    updated = t.get("updated") or t.get("completed") or 0
    parts = last.get("parts", []) or []
    textlen = sum(len(p.get("text", "")) for p in parts if isinstance(p, dict))
    reasoning_in_progress = any(
        isinstance(p, dict) and p.get("type") == "reasoning"
        and ((p.get("time") or {}).get("start") and not (p.get("time") or {}).get("end"))
        for p in parts)
    return (len(msgs), len(parts), textlen, updated), reasoning_in_progress


def _message_errored(port, pwd, sid):
    """探测最后一条 assistant 消息是否"终态但不可用"——用于快速判定会话中毒。

    True 的两种情形：
      - info.error 为真（后端显式记了 stream-error 等）；
      - info.time.completed 已置（回合已结束）且拼接的 text parts 为空（模型没产出文本）。
    进行中的消息（completed 未置）恒返回 False，不误杀正在流式产出的回合。
    GET 失败返回 False（偏保活，同 _activity_fingerprint 返回 None 的哲学）。
    """
    try:
        msgs = _serve_request("GET", port, pwd, f"/session/{sid}/message", timeout=6)
    except Exception:
        return False
    if not isinstance(msgs, list) or not msgs:
        return False
    last = msgs[-1] or {}
    info = last.get("info", {}) or {}
    if info.get("error"):
        return True
    completed = (info.get("time", {}) or {}).get("completed")
    if not completed:
        return False
    parts = last.get("parts", []) or []
    textlen = sum(len(p.get("text", "")) for p in parts if isinstance(p, dict))
    return textlen == 0


def _start_watchdog(port, pwd, sid, done, aborted):
    """启动活动感知 watchdog 线程（#75）。返回线程或 None（关闭时）。

    - done：主线程 POST 结束后 set，watchdog 随即退出（POST 快返回时 0 次 GET 探测）。
    - aborted：单元素 list，watchdog 触发 abort 时置 [True]，主线程据此抛 _IdleAbort。
    IDLE<=0 且 MAX<=0 视为完全关闭，不启 watchdog（回退无超时兜底）。
    """
    idle, mx, poll = _OPENCODE_IDLE_TIMEOUT, _OPENCODE_MAX_TIMEOUT, max(_OPENCODE_ACTIVITY_POLL, 1)
    if idle <= 0 and mx <= 0:
        return None

    def _run():
        start = time.time()
        last_active = start
        prev = None
        while not done.wait(poll):
            now = time.time()
            reason = None
            # MAX 是绝对上限：无条件先判，**即使仍在产出**也要兜底（防真卡死/失控长任务）
            if mx > 0 and now - start >= mx:
                reason = f"总时长 {int(now - start)}s(>{mx}s) 超上限"
            else:
                fp = _activity_fingerprint(port, pwd, sid)
                if fp is not None:
                    fingerprint, reasoning_in_progress = fp
                    # 仍在产出（指纹变化）或正进行未完结的 reasoning part：刷新活跃时刻
                    if reasoning_in_progress or fingerprint != prev:
                        prev, last_active = fingerprint, now
                        continue
                # 活动已停：若回合终态但坏（error/completed-空），立即 abort，不干等满 idle
                if _OPENCODE_ERROR_ABORT and _message_errored(port, pwd, sid):
                    reason = "助手消息 completed-with-error/空"
                elif idle > 0 and now - last_active >= idle:
                    reason = f"空闲 {int(now - last_active)}s(>{idle}s) 无活动"
            if reason:
                aborted[0] = True
                log(f"brain: session {sid[:12]} {reason}，abort")
                try:
                    _serve_request("POST", port, pwd, f"/session/{sid}/abort", {}, timeout=6)
                except Exception as e:
                    log(f"brain: idle abort 请求失败 {e}")
                return

    wd = threading.Thread(target=_run, name=f"oc-watchdog-{sid[:8]}", daemon=True)
    wd.start()
    return wd


# 在跑任务登记（#75 用户取消）：conv_id -> {"sid","port","pwd","title","started"}。
# 取消能力据此直接 abort，**不走 brain / 不抢 _conv_lock**，从而解阻塞正卡在
# message POST 的 worker 线程。title/started 供 /reboot 通知展示「这一停会打断什么」（#98）。
_inflight = {}
_inflight_lock = threading.Lock()


def _mark_inflight(conv_id, sid, port, pwd, title=""):
    """登记某 conv 正在跑的会话（POST 前调用）。conv_id/sid 为空则跳过。

    **started 跨轮内重建保留**：同一轮里 404 失效重建、回空重试都会再次 mark，
    若每次都重置 started，「已跑 8 分钟」会显示成刚开始，正好在最该看清耗时的时候骗人。
    title 同理：重试路径不重算 title，传空时沿用上一次的。
    """
    if not conv_id or not sid:
        return
    with _inflight_lock:
        prev = _inflight.get(conv_id)
        _inflight[conv_id] = {
            "sid": sid, "port": port, "pwd": pwd,
            "title": title or (prev or {}).get("title", ""),
            "started": (prev or {}).get("started") or time.time(),
        }


def _clear_inflight(conv_id, sid=None):
    """清理在跑登记（POST 结束 finally 调用）。指定 sid 时仅当匹配才清，避免误删新一轮。"""
    if not conv_id:
        return
    with _inflight_lock:
        rec = _inflight.get(conv_id)
        if rec and (sid is None or rec.get("sid") == sid):
            _inflight.pop(conv_id, None)


def list_inflight():
    """在跑任务快照（注册给 core.brain.list_inflight，供 /reboot 通知）。

    只吐展示需要的字段——**port/pwd 不外泄**，那是 serve 凭据，不该流进通知渲染路径。
    """
    with _inflight_lock:
        return [{"conv_id": cid, "sid": rec.get("sid", ""),
                 "title": rec.get("title", ""), "started": rec.get("started", 0)}
                for cid, rec in _inflight.items()]


def cancel_inflight(conv_id):
    """供取消能力调用：abort 该 conv 正在跑的会话。返回是否命中在跑任务。"""
    if not conv_id:
        return False
    with _inflight_lock:
        rec = _inflight.get(conv_id)
    if not rec:
        return False
    sid, port, pwd = rec.get("sid"), rec.get("port"), rec.get("pwd")
    log(f"brain: 用户取消 conv={conv_id[:12]} sid={str(sid)[:12]}")
    try:
        _serve_request("POST", port, pwd, f"/session/{sid}/abort", {}, timeout=6)
    except Exception as e:
        log(f"brain: cancel abort 请求失败 {e}")
    return True


def _post_message(port, pwd, sid, prompt, provider, model_id):
    """向 session 发一条 message，拼接 text parts 返回回复文本和统计信息。

    返回 (reply_text, usage_dict)，usage_dict 包含 input/output/reasoning/cache tokens。

    活动感知超时（#75）：POST socket 超时放大到 MAX 兜底，外挂 watchdog 轮询 session 活动；
    只要仍在产出就不 abort。watchdog 触发 abort → 抛 _IdleAbort（上层判 failed，不回退 CLI）。
    """
    done = threading.Event()
    aborted = [False]
    wd = _start_watchdog(port, pwd, sid, done, aborted)
    try:
        d = _serve_request(
            "POST", port, pwd, f"/session/{sid}/message",
            {
                "model": {"providerID": provider, "modelID": model_id},
                "system": _SYSTEM_PROMPT,
                "parts": [{"type": "text", "text": prompt}],
            },
            timeout=_OPENCODE_SOCK_TIMEOUT,
        ) or {}
    finally:
        done.set()
        if wd:
            wd.join(timeout=2)
    if aborted[0]:
        raise _IdleAbort(
            f"session {sid[:12]} aborted (idle>{_OPENCODE_IDLE_TIMEOUT}s / max>{_OPENCODE_MAX_TIMEOUT}s)")
    reply = "".join(
        p.get("text", "") for p in d.get("parts", []) if p.get("type") == "text"
    ).strip()

    # 提取 token 使用统计（支持多种格式）
    # 格式1: info.tokens (opencode serve 实际格式)
    info = d.get("info", {}) or {}
    info_tokens = info.get("tokens", {}) or {}

    # 回合以错误收尾（HTTP 仍 200）：serve 把失败正常返回，info.error 记着原因
    # （典型：模型网关 APIError "Cannot connect to API"）。无文本产出就抛 _ServeTurnError
    # → 上层判 failed（兜底提示 + ack 落失败终态），绝不伪装成空回复静默吞掉。
    err = info.get("error")
    if err and not reply:
        emsg = (err.get("data") or {}).get("message") or err.get("name") or str(err)
        raise _ServeTurnError(f"session {sid[:12]} turn error: {emsg}")

    # 格式2: tokens (参考项目格式，可能用于 SSE 事件)
    tokens = d.get("tokens", {}) or {}

    # 格式3: usage (驼峰命名，备用)
    usage = d.get("usage", {}) or {}

    # 优先使用 info.tokens（实际响应格式），然后 fallback
    cache = info_tokens.get("cache") or tokens.get("cache", {}) or {}
    # 工具调用轮次（#76）：本条消息里 type 以 "tool" 开头的 part 数，best-effort（schema 不含则 0）
    tool_calls = sum(
        1 for p in d.get("parts", [])
        if isinstance(p, dict) and str(p.get("type", "")).startswith("tool")
    )
    return reply, {
        "input_tokens": info_tokens.get("input") or tokens.get("input") or usage.get("inputTokens", 0),
        "output_tokens": info_tokens.get("output") or tokens.get("output") or usage.get("outputTokens", 0),
        "reasoning_tokens": info_tokens.get("reasoning") or tokens.get("reasoning") or usage.get("reasoningTokens", 0),
        "cache_read": cache.get("read") or usage.get("cacheReadTokens", 0),
        "cache_write": cache.get("write") or usage.get("cacheWriteTokens", 0),
        "tool_calls": tool_calls,
    }


def _brain_opencode_http(prompt, ctx=None, model=None):
    """走 opencode serve HTTP 生成回复。

    两种会话语义（AGENT_SESSION_REUSE 开关）：
      - 关（默认，旧语义）：每条消息建临时 session → POST message → 删 session。无状态、
        互不污染，但没有跨消息记忆。
      - 开（#56）：同一 conv 复用 session（serve 自带多轮历史）。命中未过期 sid 直接复用；
        POST 遇 404（session 被 serve 清了/重启失效）→ 删记录、重建一次重试。会话闲置 TTL
        过期或 LRU 逐出时删远端 session。同一 conv 串行（_conv_lock），不同 conv 并行。
      - 开 + 本轮模型 ≠ 默认（#117 跟进）：**不进**复用 session，单独建一个一次性 session
        跑完即删。provider 的 prompt cache 按模型分桶，换模型 = 整段历史重编码（见下），
        仍持 _conv_lock 保证同 conv 串行。

    两种模式都把 sid（连同来源 conv ctx）登记到 core.brain 注册表，供 text_reply 抑制 SSE
    业务通知 + question/permission 把提问/审批路由回来源群。若该轮 agent 调 question/permission
    工具，POST 阻塞到用户答复（另一线程 POST reply 解阻塞），故 timeout 需覆盖等待时间。

    Returns: 回复文本（可能空串）；serve 不可用/出错返回 None（交给调用方回退 CLI）。

    model: 本轮生效的模型（#117，None=用 _OPENCODE_MODEL）。非默认模型的轮次被分流到独立
           一次性 session（见上），因此**看不到本会话前文**——这是明确接受的取舍。
    """
    pid, port, pwd = find_serve_credentials()
    if not port:
        return None  # serve 没起或凭据缺失 → 回退
    conv_id = (ctx or {}).get("conv_id", "")
    # 逐轮换模型（#117 跟进）不进复用 session：provider 的 prompt cache **按模型分桶**，
    # 主模型在复用 session 里攒下的长上下文对另一个模型一次都不命中，整段历史要重新编码。
    # 线上实测同一 session：主模型轮 input=303/cache_read=59392，紧接的 flash 轮变成
    # input=57557/cache_read=1024；而**全新** session 只需重编 system+tools 前缀（provider
    # 侧跨 session 命中，实测 input 250~2700）。为省钱开的开关反而更贵，且 flash 轮继承历史
    # 还会照抄上一轮答案。
    # 判据是「≠ 默认模型」而非「== FLASH」：FLASH 误配成和默认同值时并没有换缓存桶，
    # 不该白丢上下文；也能自然覆盖以后新增的逐轮模型。
    # 仍持 _conv_lock：同 conv 的先后消息保持串行，_inflight 不会被并发轮次互相覆盖。
    # （_http_oneshot 自身不取 _conv_lock，threading.Lock 非重入，此处不会自锁。）
    if _SESSION_REUSE and conv_id:
        if model and model != _OPENCODE_MODEL:
            log(f"brain: 本轮模型 {model} ≠ 默认，走独立一次性 session（不复用上下文）"
                f" conv={conv_id[:12]}")
            with _conv_lock(conv_id):
                return _http_oneshot(port, pwd, prompt, ctx, model=model)
        return _http_reuse(port, pwd, conv_id, prompt, ctx, model=model)
    return _http_oneshot(port, pwd, prompt, ctx, model=model)


def _http_oneshot(port, pwd, prompt, ctx, model=None):
    """旧语义：建 → 发 → 删，无状态。"""
    model = model or _OPENCODE_MODEL
    provider, model_id = _split_model(model)
    conv_id = (ctx or {}).get("conv_id", "")
    t0 = time.time()
    sid = None
    title = _session_title(ctx, prompt)
    if model != _OPENCODE_MODEL:
        # 逐轮模型分流来的一次性 session：打个标，opencode 后台会话列表里一眼可辨，
        # 不和复用主会话混在一起（#117 跟进）
        title = f"⚡ {title}"
    try:
        sid = _create_session(port, pwd, title)
        _register_textreply_sid(sid, ctx)
        _mark_inflight(conv_id, sid, port, pwd, title)
        reply, usage = _post_message(port, pwd, sid, prompt, provider, model_id)
        _oc_log("http", model, time.time() - t0, prompt, reply, True, sess="oneshot")
        _stash_task_stats(conv_id, usage, time.time() - t0)
        if model != _OPENCODE_MODEL:
            # 逐轮模型分流轮：记进该 conv 的 flash_* 计数（无状态模式下没记录 → no-op）
            _update_flash_stats(conv_id,
                                input_tokens=usage.get("input_tokens", 0),
                                output_tokens=usage.get("output_tokens", 0))
        return reply
    except _IdleAbort as e:
        # 活动感知超时 abort：终态失败，向上传播（_brain_opencode 判 failed 不回退 CLI）
        _oc_log("http", model, time.time() - t0, prompt, "", False, str(e), sess="oneshot")
        raise
    except Exception as e:
        _oc_log("http", model, time.time() - t0, prompt, "", False, str(e), sess="oneshot")
        log(f"brain opencode http err: {e}")
        return None  # 交给调用方回退 CLI
    finally:
        _clear_inflight(conv_id, sid)
        _delete_session(port, pwd, sid)


def _http_reuse(port, pwd, conv_id, prompt, ctx, model=None):
    """复用语义：同一 conv 串行走同一 session；404 失效则重建一次重试。"""
    model = model or _OPENCODE_MODEL
    provider, model_id = _split_model(model)
    t0 = time.time()
    title = _session_title(ctx, prompt)
    with _conv_lock(conv_id):
        sid = _lookup_sid(conv_id, ctx=ctx)
        reused = sid is not None
        try:
            if sid is None:
                sid = _create_session(port, pwd, title)
            _register_textreply_sid(sid, ctx)   # 刷新 conv ctx（回程路由用最新来源）
            _mark_inflight(conv_id, sid, port, pwd, title)
            try:
                reply, usage = _post_message(port, pwd, sid, prompt, provider, model_id)
            except urllib.error.HTTPError as he:
                # 复用的 session 已被 serve 清（重启/GC）→ 丢记录、重建一次重试
                if reused and he.code == 404:
                    log(f"brain: 复用 session {sid[:12]} 失效(404)，重建 conv={conv_id[:12]}")
                    _forget_sid(conv_id)
                    sid = _create_session(port, pwd, title)
                    _register_textreply_sid(sid, ctx)
                    _mark_inflight(conv_id, sid, port, pwd, title)
                    reply, usage = _post_message(port, pwd, sid, prompt, provider, model_id)
                    reused = False  # 重建了，视为新会话
                else:
                    raise
            except _ServeTurnError:
                # 回合带错收尾且无产出：复用会话按中毒自愈重建一次重试；新会话/重试再错
                # 就向上抛（→ 回退 CLI → 仍失败则 failed，兜底提示 + ack 落失败终态）
                if reused and _OPENCODE_EMPTY_RETRY:
                    log(f"brain: 复用 session {sid[:12]} 回合错误，丢弃并新建重试 conv={conv_id[:12]}")
                    _forget_sid(conv_id)
                    _delete_session(port, pwd, sid)
                    sid = _create_session(port, pwd, title)
                    _register_textreply_sid(sid, ctx)
                    _mark_inflight(conv_id, sid, port, pwd, title)
                    reply, usage = _post_message(port, pwd, sid, prompt, provider, model_id)
                    reused = False
                else:
                    raise
            # 会话中毒自愈：复用的 session 回空（多半后端 stream-error 把回合 completed 成空），
            # 丢弃 sid + 删远端，新建一次重试——既清毒又让当前这条消息答上（仿 404 rebuild-once）。
            if _OPENCODE_EMPTY_RETRY and reused and not reply:
                log(f"brain: 复用 session {sid[:12]} 回空(疑似 stream-error)，丢弃并新建重试 conv={conv_id[:12]}")
                _forget_sid(conv_id)
                _delete_session(port, pwd, sid)
                sid = _create_session(port, pwd, title)
                _register_textreply_sid(sid, ctx)
                _mark_inflight(conv_id, sid, port, pwd, title)
                reply, usage = _post_message(port, pwd, sid, prompt, provider, model_id)
                reused = False  # 重建了，按新会话登记（下面 is_new=True）
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
            _stash_task_stats(conv_id, usage, time.time() - t0)
            _oc_log("http", model, time.time() - t0, prompt, reply, True, sess="reuse")
            return reply
        except _IdleAbort as e:
            # 活动感知超时 abort：会话已被 abort，丢记录 + 删远端，向上传播（不回退 CLI）
            _forget_sid(conv_id)
            _delete_session(port, pwd, sid)
            _oc_log("http", model, time.time() - t0, prompt, "", False, str(e), sess="reuse")
            log(f"brain: 活动感知超时 abort conv={conv_id[:12]}")
            raise
        except Exception as e:
            # 失败别把坏 sid 留在表里，避免后续消息一直命中坏会话
            _forget_sid(conv_id)
            _delete_session(port, pwd, sid)
            _oc_log("http", model, time.time() - t0, prompt, "", False, str(e), sess="reuse")
            log(f"brain opencode http err: {e}")
            return None  # 交给调用方回退 CLI
        finally:
            _clear_inflight(conv_id)


def _brain_opencode_cli(prompt, model=None):
    """回退路径：调 `opencode run <prompt> --model M --format json`，拼接 text 事件为回复。

    HTTP 不可用时的兜底。输出是 NDJSON 事件流，逐行取 type==text 的 part.text 拼接。
    失败（超时 / rc!=0 / opencode 不存在）**抛异常**，由调用方判为 failed（#59）——不再
    把失败伪装成空字符串，以便上层给用户兜底提示。
    """
    full_prompt = f"{_SYSTEM_PROMPT}\n\n{prompt}"
    model = model or _OPENCODE_MODEL
    cmd = [_OPENCODE_BIN, "run", full_prompt,
           "--model", model, "--format", "json"]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=_OPENCODE_TIMEOUT)
    if r.returncode != 0:
        _oc_log("cli", model, time.time() - t0, prompt, "", False,
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
    _oc_log("cli", model, time.time() - t0, prompt, reply, True)
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
# 同时注册状态感知实现（#59）：text_reply 经 generate_reply_ex 拿 ok/empty/failed 区分。
# 以及在跑任务快照（#98）：/reboot 据此告诉用户「这一停会打断什么」。
from core.brain import register_brain, register_brain_ex, register_inflight  # noqa: E402
register_brain(generate_reply)
register_brain_ex(generate_reply_ex)
register_inflight(list_inflight)
