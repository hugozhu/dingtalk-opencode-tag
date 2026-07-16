"""brain.py — 数字员工的"大脑"：把用户消息生成回复文本（custom 层）

可插拔后端，由环境变量 AGENT_BRAIN 选择：
  echo     (默认)  零依赖，规则式回复。用于打通收发闭环、无网络/无 LLM 也能跑。
  opencode         调本机 `opencode run` 一次性生成回复（无需托管 serve，免鉴权可用
                   opencode/*-free 模型）。已实测可用。
  proxy            经 agent_common.PROXY_URL 调用 LLM /chat/completions 生成回复。

为什么默认 echo：本机未必装 opencode / LLM proxy 未必可达。默认走 echo 保证 pipeline
今天就能端到端验证；配好后设 AGENT_BRAIN=opencode 或 proxy 即切换。

接口：generate_reply(user, text, ctx=None) -> str（返回空串表示不回复）
"""

import json
import os
import subprocess
import urllib.request

from core.agent_common import PROXY_URL, PROXY_KEY, log

# 大脑后端选择
_BRAIN = os.environ.get("AGENT_BRAIN", "echo")
# proxy 后端用的对话模型（区别于 VISION_MODEL）
_CHAT_MODEL = os.environ.get("AGENT_CHAT_MODEL", "gpt-4o-mini")
# opencode 后端用的模型（provider/model 格式；免鉴权可用 *-free）
_OPENCODE_MODEL = os.environ.get("AGENT_OPENCODE_MODEL", "opencode/deepseek-v4-flash-free")
_OPENCODE_BIN = os.environ.get("AGENT_OPENCODE_BIN", "opencode")
_OPENCODE_TIMEOUT = int(os.environ.get("AGENT_OPENCODE_TIMEOUT", "90"))
# 系统提示词（proxy/opencode 后端），可用环境变量覆盖
_SYSTEM_PROMPT = os.environ.get(
    "AGENT_SYSTEM_PROMPT",
    "你是一个数字员工助手，在钉钉群里回答同事的问题。回答简洁、准确、友好，用中文。",
)
# 回复长度上限（防止刷屏）
_MAX_REPLY_CHARS = int(os.environ.get("AGENT_MAX_REPLY_CHARS", "1000"))


def generate_reply(user, text, ctx=None):
    """根据用户消息生成回复文本。返回空串 = 不回复。

    Args:
        user: 发送者展示名
        text: 消息正文（已 strip）
        ctx:  可选上下文 dict（conv_id / msg_id / conv_type 等）
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        if _BRAIN == "proxy":
            reply = _brain_proxy(user, text, ctx)
        elif _BRAIN == "opencode":
            reply = _brain_opencode(user, text, ctx)
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
# opencode 后端 — 调本机 `opencode run`（一次性、无需托管 serve）
# ---------------------------------------------------------------------------

def _brain_opencode(user, text, ctx):
    """调 `opencode run <prompt> --model M --format json`，拼接 text 事件为回复。

    优点：不需要托管 opencode serve / 管理凭据；免鉴权可用 opencode/*-free 模型。
    输出是 NDJSON 事件流，逐行取 type==text 的 part.text 拼接。
    """
    prompt = f"{_SYSTEM_PROMPT}\n\n{user}：{text}"
    cmd = [_OPENCODE_BIN, "run", prompt,
           "--model", _OPENCODE_MODEL, "--format", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=_OPENCODE_TIMEOUT)
    if r.returncode != 0:
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
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# proxy 后端 — 经 LLM /chat/completions
# ---------------------------------------------------------------------------

def _brain_proxy(user, text, ctx):
    """调用 LLM 生成回复（OpenAI 兼容 /chat/completions）。"""
    body = json.dumps({
        "model": _CHAT_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"{user}：{text}"},
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
