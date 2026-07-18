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

import os
import re
import tempfile
import threading
from collections import OrderedDict

from core.agent_common import _proxy_vision, _run_cli, log, submit_handler
from core.capabilities import Capability, register
from core.inbound import KIND_IMAGE
from custom.brain import generate_reply
from custom.replier import send_reply

# 图片 mediaId 提取（content 形如 "[图片消息](mediaId=$xxx)"，ID 含 $@/_- 等，止于 )）
_RE_MEDIA_ID = re.compile(r"mediaId=([^\s)]+)")
# 去掉 content 里的图片标记，剩下的当用户 caption（"[图片消息](mediaId=x)看标红处" → "看标红处"）
_RE_IMAGE_TAG = re.compile(r"\[图片消息\]\(mediaId=[^\s)]+\)")

# 防回环：数字员工自己发的图不处理
_SELF_NAMES = {
    n.strip() for n in os.environ.get("AGENT_SELF_NAMES", "数字员工,Claude Code").split(",")
    if n.strip()
}

# msgId 去重（断线重连可能重投）—— 有界 FIFO
_seen = OrderedDict()
_seen_lock = threading.Lock()
_SEEN_MAX = 2048

# prompt 末句：点明这是图片、内容是 vision 识别的，让 agent 基于内容回应（别再说看不到图）
_IMAGE_PROMPT_FOOTER = os.environ.get(
    "CAP_IMAGE_PROMPT_FOOTER",
    "以上「图片识别内容」由多模态模型从用户发送的图片中提取/描述得到（你本身看不到原图，"
    "但可据此内容回应）。请结合用户随图的说明（若有），对用户的意图做出有帮助的回应。",
)


def _seen_before(msg_id):
    if not msg_id:
        return False
    with _seen_lock:
        if msg_id in _seen:
            return True
        _seen[msg_id] = None
        if len(_seen) > _SEEN_MAX:
            _seen.popitem(last=False)
    return False


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


def _recognize(image_path):
    """读图片字节 → vision 识别，返回描述文本（失败返回 ""）。用完删临时文件。"""
    try:
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        return _proxy_vision(img_bytes)
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

    # 组 prompt：识别内容 + 用户 caption + 末句指令
    parts = [f"用户 {user} 发送了一张图片。", "", "【图片识别内容】", desc, ""]
    if caption:
        parts += [f"【用户随图说明】{caption}", ""]
    parts.append(_IMAGE_PROMPT_FOOTER)
    prompt = "\n".join(parts)

    reply = generate_reply(user, prompt, raw=True)
    if reply:
        send_reply(conv_id, conv_type, reply)
    else:
        log(f"image: 大脑无回复 msgId={msg_id[:24]}")


def on_inbound(msg):
    """图片消息入站：防回环 → 去重 → 提交 handle_image。返回 True=已消费。"""
    if msg.user in _SELF_NAMES:
        return True
    if _seen_before(msg.msg_id):
        return True
    submit_handler(handle_image, msg.user, msg.text, msg.msg_id, msg.conv_id, msg.conv_type)
    return True


CAPABILITY = Capability(
    name="image",
    on_inbound=on_inbound,
    handles_kinds={KIND_IMAGE},
    priority=40,             # 图片检测最明确，先于转发(50)/文本(100)
    default_enabled=True,
)
register(CAPABILITY)
