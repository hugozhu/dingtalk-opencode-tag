"""audio — 语音消息识别能力（custom 插件）

收到语音消息时：提取 mediaId → 下载语音 → Whisper ASR 转文字 → 组装 prompt 注入 brain → 回复发回来源群。

在 dws event consume 模型下，语音消息以文本到达，content 形如
`[语音消息](mediaId=<ID>)`。core 的 inbound.classify 已把这类识别为 kind=audio；
本能力挂 on_inbound(kind=audio)。

流程：
  1. on_inbound(kind=audio)：防回环 → 去重 → 提交 handle_audio。
  2. handle_audio：提取 mediaId → download-media 下载到临时文件 → Whisper ASR 识别 →
     组 prompt（转录文本）→ brain(raw) 生成回复 → send_reply 回来源群。
  3. ASR 不可用/识别失败 → 明确告知（而非静默），避免用户以为没收到。

开关：CAP_AUDIO_ENABLED（默认开）。优先级 40（与图片同级，先于转发 50 / 文本 100）。
"""

import os
import re
import shutil
import tempfile

from core.agent_common import _run_cli, log, submit_handler
from core.capabilities import Capability, register
from core.inbound import KIND_AUDIO
from core.brain import generate_reply
from core.replier import send_reply

# 语音 mediaId 提取（content 形如 "[语音消息](mediaId=$xxx)"，ID 含 $@/_- 等，止于 )）
_RE_MEDIA_ID = re.compile(r"mediaId=([^\s)]+)")

# Whisper 模型配置
_WHISPER_MODEL = os.environ.get("AGENT_AUDIO_WHISPER_MODEL", "base")  # tiny/base/small/medium
_WHISPER_LANGUAGE = os.environ.get("AGENT_AUDIO_WHISPER_LANGUAGE", "zh")  # zh/en/auto
_WHISPER_TIMEOUT = int(os.environ.get("CAP_AUDIO_WHISPER_TIMEOUT", "120"))  # ASR 超时（秒）

# 转录 prompt 末句：点明这是语音、内容是 ASR 识别的
_AUDIO_PROMPT_FOOTER = os.environ.get(
    "CAP_AUDIO_PROMPT_FOOTER",
    "以上「语音转录内容」由语音识别模型从用户发送的语音中转录得到。\n"
    "请根据转录内容理解用户意图，并做出有帮助的回应。",
)


def _download_audio(media_id, msg_id, conv_id):
    """download-media 下载语音到临时文件，返回 (audio_path, tmp_dir) 或 (None, None)。

    调用方负责在用完后 shutil.rmtree(tmp_dir)。
    """
    tmp_dir = tempfile.mkdtemp(prefix="agent_audio_")
    rc, _ = _run_cli([
        "chat", "message", "download-media",
        "--type", "mediaId",
        "--resource-id", media_id,
        "--message-id", msg_id,
        "--open-conversation-id", conv_id,
        "--output", tmp_dir + "/",
    ], timeout=30)
    if rc != 0:
        log(f"audio: 下载失败 rc={rc} mediaId={media_id[:24]}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, None
    for name in os.listdir(tmp_dir):
        return os.path.join(tmp_dir, name), tmp_dir
    log(f"audio: 下载目录为空 mediaId={media_id[:24]}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None, None


def _transcribe_whisper(audio_path, tmp_dir):
    """用 Whisper 转录语音文件，返回转录文本（失败返回 ""）。用完删临时目录。"""
    try:
        import whisper
        log(f"audio: 加载 Whisper 模型 {_WHISPER_MODEL}...")
        model = whisper.load_model(_WHISPER_MODEL)

        log(f"audio: 开始转录 {os.path.basename(audio_path)}...")
        language = None if _WHISPER_LANGUAGE == "auto" else _WHISPER_LANGUAGE
        result = model.transcribe(audio_path, language=language)

        text = (result.get("text") or "").strip()
        detected_lang = result.get("language", "unknown")
        log(f"audio: 转录成功 lang={detected_lang} text_len={len(text)}")
        return text
    except ImportError:
        log("audio: Whisper 未安装，请运行: pip3 install openai-whisper")
        return ""
    except Exception as e:
        log(f"audio: Whisper 转录失败 {e}")
        return ""
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def handle_audio(user, text, msg_id, conv_id, conv_type):
    """提取 mediaId → 下载 → Whisper 转录 → 组 prompt → brain 回复 → 发回来源群。"""
    mid_m = _RE_MEDIA_ID.search(text or "")
    if not mid_m:
        log(f"audio: 未提取到 mediaId msgId={msg_id[:24]}")
        return
    media_id = mid_m.group(1)

    # 下载语音文件
    audio_path, tmp_dir = _download_audio(media_id, msg_id, conv_id)
    if not audio_path:
        send_reply(conv_id, conv_type, "抱歉，这条语音我没能下载下来，能再发一次吗？")
        return

    # 使用 Whisper 转录
    transcription = _transcribe_whisper(audio_path, tmp_dir)
    if not transcription:
        send_reply(conv_id, conv_type,
                   "抱歉，语音识别失败了（可能是识别服务不可达）。你可以把内容用文字发我。")
        return

    log(f"audio: msgId={msg_id[:24]} 转录成功 text_len={len(transcription)}")

    # 结构化呈现语音转录结果（用户+语音标识+转录内容代码块+任务指令）
    parts = [
        f"用户 {user} 发送了一条语音消息。",
        "",
        "【语音转录内容】",
        "```",
        transcription,
        "```",
        "",
        _AUDIO_PROMPT_FOOTER,
    ]
    prompt = "\n".join(parts)

    reply = generate_reply(user, prompt, raw=True, ctx={
        "conv_id": conv_id, "conv_type": conv_type, "msg_id": msg_id, "user": user,
    })
    if reply:
        send_reply(conv_id, conv_type, reply)
    else:
        log(f"audio: 大脑无回复 msgId={msg_id[:24]}")


def on_inbound(msg):
    """语音消息入站：提交 handle_audio。返回 True=已消费。

    防回环 + msgId 去重由 core dispatch_inbound（loop_guard/dedup 声明）处理。
    """
    submit_handler(handle_audio, msg.user, msg.text, msg.msg_id, msg.conv_id, msg.conv_type)
    return True


CAPABILITY = Capability(
    name="audio",
    on_inbound=on_inbound,
    handles_kinds={KIND_AUDIO},
    priority=40,             # 与图片同级，语音检测明确，先于转发(50)/文本(100)
    default_enabled=True,
    loop_guard=True,         # core 统一防回环
    dedup=True,              # core 统一 msgId 去重
)
register(CAPABILITY)
