#!/usr/bin/env python3
"""test_audio_capability.py — 语音消息能力单元测试"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# 添加 src 到模块搜索路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from core.inbound import KIND_AUDIO, InboundMessage, classify
from custom.capabilities import audio


class TestAudioClassify(unittest.TestCase):
    """测试 inbound.classify 对语音消息的识别"""

    def test_audio_message_recognized(self):
        """语音消息格式应被识别为 KIND_AUDIO"""
        text = "[语音消息](mediaId=@lR_PJx04cRJCn7sAALCCe9dVk0p1Jwo-ZOVTzhkA) 注意：如需下载使用dws chat message download-media命令下载"
        self.assertEqual(classify(text), KIND_AUDIO)

    def test_audio_message_simple(self):
        """简化的语音消息格式"""
        text = "[语音消息](mediaId=@abc123)"
        self.assertEqual(classify(text), KIND_AUDIO)

    def test_not_audio_message(self):
        """非语音消息不应被识别为语音"""
        self.assertNotEqual(classify("普通文本"), KIND_AUDIO)
        self.assertNotEqual(classify("[图片消息](mediaId=@abc)"), KIND_AUDIO)
        self.assertNotEqual(classify("[文件] test.txt"), KIND_AUDIO)


class TestAudioCapability(unittest.TestCase):
    """测试语音能力处理逻辑"""

    def setUp(self):
        """设置测试环境"""
        self.msg = InboundMessage(
            user="testuser",
            text="[语音消息](mediaId=@test123)",
            conv_type="1",
            conv_id="cidTest",
            msg_id="msgTest",
            kind=KIND_AUDIO,
            raw_line="[connect] 收到 @testuser: [语音消息](mediaId=@test123) (convType=1 convId=cidTest msgId=msgTest)",
        )

    def test_extract_media_id(self):
        """测试 mediaId 提取"""
        match = audio._RE_MEDIA_ID.search(self.msg.text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "@test123")

    @patch.object(audio, "submit_handler")
    def test_on_inbound_submits_handler(self, mock_submit):
        """测试 on_inbound 提交处理函数"""
        result = audio.on_inbound(self.msg)
        self.assertTrue(result)  # 返回 True 表示已消费
        mock_submit.assert_called_once()
        # 验证参数
        args = mock_submit.call_args[0]
        self.assertEqual(args[0], audio.handle_audio)
        self.assertEqual(args[1], "testuser")
        self.assertEqual(args[2], "[语音消息](mediaId=@test123)")

    @patch.object(audio, "send_reply")
    @patch.object(audio, "_download_audio", return_value=(None, None))
    def test_download_failure_sends_error(self, mock_download, mock_send):
        """测试下载失败时发送错误提示"""
        audio.handle_audio("user", self.msg.text, "msgId", "convId", "1")
        mock_send.assert_called_once_with("convId", "1", "抱歉，这条语音我没能下载下来，能再发一次吗？")

    @patch.object(audio, "send_reply")
    @patch.object(audio, "generate_reply", return_value="")
    @patch.object(audio, "_transcribe_whisper", return_value="")
    @patch.object(audio, "_download_audio")
    def test_transcribe_failure_sends_error(self, mock_download, mock_transcribe, mock_generate, mock_send):
        """测试转录失败时发送错误提示"""
        tmp_dir = tempfile.mkdtemp()
        audio_path = os.path.join(tmp_dir, "test.amr")
        with open(audio_path, "wb") as f:
            f.write(b"fake audio")
        mock_download.return_value = (audio_path, tmp_dir)

        audio.handle_audio("user", self.msg.text, "msgId", "convId", "1")
        mock_send.assert_called_once_with(
            "convId", "1",
            "抱歉，语音识别失败了（可能是识别服务不可达）。你可以把内容用文字发我。"
        )

    @patch.object(audio, "send_reply")
    @patch.object(audio, "generate_reply", return_value="这是回复")
    @patch.object(audio, "_transcribe_whisper", return_value="你好世界")
    @patch.object(audio, "_download_audio")
    def test_successful_transcription_generates_reply(self, mock_download, mock_transcribe, mock_generate, mock_send):
        """测试成功转录后生成回复"""
        tmp_dir = tempfile.mkdtemp()
        audio_path = os.path.join(tmp_dir, "test.amr")
        with open(audio_path, "wb") as f:
            f.write(b"fake audio")
        mock_download.return_value = (audio_path, tmp_dir)

        audio.handle_audio("user", self.msg.text, "msgId", "convId", "1")

        # 验证 generate_reply 被调用，并且 prompt 包含转录文本
        mock_generate.assert_called_once()
        call_args = mock_generate.call_args
        prompt = call_args[0][1]  # 第二个位置参数是 prompt
        self.assertIn("你好世界", prompt)
        self.assertIn("语音转录内容", prompt)

        # 验证发送回复
        mock_send.assert_called_once_with("convId", "1", "这是回复")


class TestCapabilityRegistration(unittest.TestCase):
    """测试能力注册"""

    def test_capability_registered(self):
        """测试能力已正确注册"""
        cap = audio.CAPABILITY
        self.assertEqual(cap.name, "audio")
        self.assertEqual(cap.priority, 40)
        self.assertTrue(cap.default_enabled)
        self.assertTrue(cap.loop_guard)
        self.assertTrue(cap.dedup)
        self.assertIn(KIND_AUDIO, cap.handles_kinds)


if __name__ == "__main__":
    # 运行测试
    unittest.main(verbosity=2)
