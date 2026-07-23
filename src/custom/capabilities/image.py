"""image — 图片消息识别能力（custom 插件）

主模型是文本模型，看不到图片。收到图片消息时：提取 mediaId → 下载图片 →
多模态模型（vision，经 proxy）识别内容 → 组装 prompt 注入 brain → 回复发回来源群。

在 dws event consume 模型下，图片消息以文本到达，content 形如
`[图片消息](mediaId=<ID>)`（可能后面跟一段说明文字/caption）。core 的 inbound.classify
已把这类识别为 kind=image；本能力挂 on_inbound(kind=image)。

流程：
  1. on_inbound(kind=image)：防回环 → 去重 → 提交 handle_image。
  2. handle_image：提取 mediaId → download-media 下载到临时文件 → _proxy_vision 识别 →
     组 prompt（识别文本 + 用户 caption）→ brain(raw) 生成回复 → send_reply 回来源群。
  3. vision 不可用/识别失败 → 明确告知（而非静默），避免用户以为没收到。

开关：CAP_IMAGE_ENABLED（默认开）。优先级 40（先于合并转发 50 / 文本 100，图片检测最明确）。
"""

import base64
import os
import re
import tempfile

from core.agent_common import _proxy_vision, _run_cli, find_serve_credentials, log, serve_request, submit_handler
from core.capabilities import Capability, register
from core.inbound import KIND_IMAGE
from core.brain import generate_reply, register_session as _register_textreply_sid
from core.replier import send_reply

# 图片 mediaId 提取（content 形如 "[图片消息](mediaId=$xxx)"，ID 含 $@/_- 等，止于 )）
_RE_MEDIA_ID = re.compile(r"mediaId=([^\s)]+)")
# 去掉 content 里的图片标记，剩下的当用户 caption（"[图片消息](mediaId=x)看标红处" → "看标红处"）
_RE_IMAGE_TAG = re.compile(r"\[图片消息\]\(mediaId=[^\s)]+\)")

# 识别方式：优先经 opencode serve 用免费多模态模型识别（无需外部 proxy）；失败回退
# agent_common._proxy_vision（外部 PROXY_URL）。AGENT_VISION_MODEL 是 provider/model 格式。
_VISION_MODEL = os.environ.get("AGENT_VISION_MODEL", "")
_VISION_TIMEOUT = int(os.environ.get("CAP_IMAGE_VISION_TIMEOUT", "90"))
# 识别 prompt（要模型逐字提取文字 + 客观描述，不主观总结）
_VISION_PROMPT = os.environ.get(
    "CAP_IMAGE_VISION_PROMPT",
    "请逐字提取这张图片中的所有文字内容（保持原始顺序、换行、标点，不要省略或总结）。"
    "如果图片中没有文字，则客观描述图片内容（场景、物体、UI 元素、图表数据、颜色等），"
    "不要做主观总结或解读。",
)

# msgId 去重（断线重连可能重投）+ 防回环由 core 声明式处理（见 Capability(dedup/loop_guard)）。

# prompt 末句：点明这是图片、内容是 vision 识别的，参考生产版让 agent 理解多模态语境。
_IMAGE_PROMPT_FOOTER = os.environ.get(
    "CAP_IMAGE_PROMPT_FOOTER",
    "以上「图片识别内容」由多模态模型从用户发送的图片中提取/描述得到。\n"
    "你本身看不到原图，但可根据识别内容理解用户意图。\n"
    "请结合用户随图的说明（若有），对用户发送这张图片的意图做出有帮助的回应。",
)


def _download_image(media_id, msg_id, conv_id):
    """download-media 下载图片到临时文件，返回本地路径或 None。"""
    tmp_dir = tempfile.mkdtemp(prefix="agent_img_")
    rc, _ = _run_cli([
        "chat", "message", "download-media",
        "--type", "mediaId",
        "--resource-id", media_id,
        "--message-id", msg_id,
        "--open-conversation-id", conv_id,
        "--output", tmp_dir + "/",
    ], timeout=30)
    if rc != 0:
        log(f"image: 下载失败 rc={rc} mediaId={media_id[:24]}")
        return None
    for name in os.listdir(tmp_dir):
        return os.path.join(tmp_dir, name)
    log(f"image: 下载目录为空 mediaId={media_id[:24]}")
    return None


def _split_model(model):
    """'provider/model' → (providerID, modelID)。无 '/' 时 provider 空。"""
    if "/" in (model or ""):
        p, _, m = model.partition("/")
        return p, m
    return "", (model or "")


def _recognize_via_serve(img_bytes, mime="image/png"):
    """经 opencode serve 用 AGENT_VISION_MODEL 识别图片，返回描述文本或 ""。

    建临时 session → POST 一条含 file(image) + text 的 user 消息（阻塞到该轮完成）→
    取 assistant 文本 → best-effort 删 session。免外部 proxy。
    """
    if not _VISION_MODEL:
        return ""
    pid, port, pwd = find_serve_credentials()
    if not port:
        log("image: serve 凭据缺失，serve 识别不可用")
        return ""
    provider, model_id = _split_model(_VISION_MODEL)
    b64 = base64.b64encode(img_bytes).decode()
    data_url = f"data:{mime};base64,{b64}"

    sid = None
    try:
        created = serve_request("POST", "/session", {"title": "agent-vision"},
                                timeout=10, port=port, pwd=pwd)
        sid = (created or {}).get("id")
        if not sid:
            return ""
        # 登记为 brain 抑制名单，避免 vision session 的 SSE 事件触发业务通知刷屏
        _register_textreply_sid(sid)
        d = serve_request("POST", f"/session/{sid}/message", {
            "model": {"providerID": provider, "modelID": model_id},
            "parts": [
                {"type": "file", "mime": mime, "filename": "image", "url": data_url},
                {"type": "text", "text": _VISION_PROMPT},
            ],
        }, timeout=_VISION_TIMEOUT, port=port, pwd=pwd) or {}
        desc = "".join(
            p.get("text", "") for p in d.get("parts", []) if p.get("type") == "text"
        ).strip()
        if desc:
            log(f"image: serve 识别成功 model={_VISION_MODEL} desc_len={len(desc)}")
        return desc
    except Exception as e:
        log(f"image: serve 识别失败 model={_VISION_MODEL} err={e}")
        return ""
    finally:
        if sid:
            try:
                serve_request("DELETE", f"/session/{sid}", timeout=6, port=port, pwd=pwd)
            except Exception:
                pass


def _recognize(image_path):
    """读图片字节 → vision 识别，返回描述文本（失败返回 ""）。用完删临时文件。

    优先经 opencode serve 用 AGENT_VISION_MODEL 识别（免外部 proxy）；空则回退
    agent_common._proxy_vision（外部 PROXY_URL）。
    """
    mime = "image/jpeg" if image_path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    try:
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        desc = _recognize_via_serve(img_bytes, mime=mime)
        if not desc:
            desc = _proxy_vision(img_bytes)   # 回退外部 proxy
        return desc
    except Exception as e:
        log(f"image: 识别读文件失败 {e}")
        return ""
    finally:
        try:
            os.unlink(image_path)
        except Exception:
            pass


def handle_image(user, text, msg_id, conv_id, conv_type):
    """提取 mediaId → 下载 → vision 识别 → 组 prompt → brain 回复 → 发回来源群。"""
    mid_m = _RE_MEDIA_ID.search(text or "")
    if not mid_m:
        log(f"image: 未提取到 mediaId msgId={msg_id[:24]}")
        return
    media_id = mid_m.group(1)
    caption = _RE_IMAGE_TAG.sub("", text or "").strip()  # 图外的说明文字

    image_path = _download_image(media_id, msg_id, conv_id)
    if not image_path:
        send_reply(conv_id, conv_type, "抱歉，这张图片我没能下载下来，能再发一次吗？")
        return

    desc = _recognize(image_path)
    if not desc:
        send_reply(conv_id, conv_type,
                   "抱歉，图片内容识别失败了（可能是识别服务不可达）。你可以把关键内容用文字发我。")
        return

    log(f"image: msgId={msg_id[:24]} 识别成功 desc_len={len(desc)} caption={caption[:30]!r}")

    # 参考生产版：结构化呈现图片识别结果（用户+图片标识+识别内容代码块+用户说明+任务指令）
    parts = [
        f"用户 {user} 发送了一张图片。",
        "",
        "【图片识别内容】",
        "```",
        desc,
        "```",
        "",
    ]
    if caption:
        parts += [f"【用户随图说明】{caption}", ""]
    parts.append(_IMAGE_PROMPT_FOOTER)
    prompt = "\n".join(parts)

    reply = generate_reply(user, prompt, raw=True, ctx={
        "conv_id": conv_id, "conv_type": conv_type, "msg_id": msg_id, "user": user,
    })
    if reply:
        send_reply(conv_id, conv_type, reply)
    else:
        log(f"image: 大脑无回复 msgId={msg_id[:24]}")


def on_inbound(msg):
    """图片消息入站：提交 handle_image。返回 True=已消费。

    防回环 + msgId 去重由 core dispatch_inbound（loop_guard/dedup 声明）处理。
    """
    submit_handler(handle_image, msg.user, msg.text, msg.msg_id, msg.conv_id, msg.conv_type)
    return True


CAPABILITY = Capability(
    name="image",
    on_inbound=on_inbound,
    handles_kinds={KIND_IMAGE},
    priority=40,             # 图片检测最明确，先于转发(50)/文本(100)
    default_enabled=True,
    loop_guard=True,         # core 统一防回环
    dedup=True,              # core 统一 msgId 去重
)
register(CAPABILITY)
