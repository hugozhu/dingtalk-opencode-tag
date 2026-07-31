#!/bin/bash
# e2e_audio_test.sh — 语音消息端到端测试
#
# 用法：bash tests/custom/e2e_audio_test.sh [audio_file]
#
# 如果未提供音频文件，脚本将跳过实际发送测试（仅验证组件加载）。
# 提供音频文件时，会将其作为语音消息发送给数字员工并验证回复。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# 加载配置
source "$PROJECT_DIR/config/constants.sh"
if [[ -f "$PROJECT_DIR/config/constants.local.sh" ]]; then
    source "$PROJECT_DIR/config/constants.local.sh"
fi

echo "========================================"
echo "语音消息能力 E2E 测试"
echo "========================================"
echo

# 1. 验证 Whisper 已安装
echo "✓ 检查 Whisper 安装..."
if ! python3 -c "import whisper" 2>/dev/null; then
    echo "✗ Whisper 未安装"
    echo "  请运行: pip3 install openai-whisper"
    exit 1
fi
echo "  ✓ Whisper 已安装"

# 2. 验证 Whisper 模型已下载
echo "✓ 检查 Whisper 模型..."
WHISPER_MODEL="${AGENT_AUDIO_WHISPER_MODEL:-base}"
if python3 -c "import whisper; whisper.load_model('$WHISPER_MODEL')" 2>/dev/null; then
    echo "  ✓ Whisper 模型 '$WHISPER_MODEL' 可用"
else
    echo "  ✗ Whisper 模型 '$WHISPER_MODEL' 加载失败"
    exit 1
fi

# 3. 验证能力已启用
echo "✓ 检查语音能力配置..."
CAP_AUDIO_ENABLED="${CAP_AUDIO_ENABLED:-1}"
if [[ "$CAP_AUDIO_ENABLED" != "1" ]]; then
    echo "  ✗ 语音能力已禁用 (CAP_AUDIO_ENABLED=$CAP_AUDIO_ENABLED)"
    echo "  请在 config/constants.local.sh 中设置: export CAP_AUDIO_ENABLED=1"
    exit 1
fi
echo "  ✓ 语音能力已启用"

# 4. 验证能力已注册
echo "✓ 检查能力注册..."
if python3 -c "
import sys
sys.path.insert(0, '$PROJECT_DIR/src')
from custom.capabilities import audio
cap = audio.CAPABILITY
assert cap.name == 'audio', f'capability name mismatch: {cap.name}'
assert cap.priority == 40, f'capability priority mismatch: {cap.priority}'
print('  ✓ audio capability 已正确注册')
"; then
    : # 成功
else
    echo "  ✗ 能力注册验证失败"
    exit 1
fi

# 5. 验证 inbound 分类
echo "✓ 检查消息分类..."
python3 -c "
import sys
sys.path.insert(0, '$PROJECT_DIR/src')
from core.inbound import classify, KIND_AUDIO

text = '[语音消息](mediaId=@test123)'
kind = classify(text)
assert kind == KIND_AUDIO, f'classify failed: expected {KIND_AUDIO}, got {kind}'
print('  ✓ 语音消息分类正确')
"

echo
echo "========================================"
echo "✅ 所有组件检查通过"
echo "========================================"
echo

# 6. 实际发送测试（可选）
if [[ $# -ge 1 ]]; then
    AUDIO_FILE="$1"
    if [[ ! -f "$AUDIO_FILE" ]]; then
        echo "✗ 音频文件不存在: $AUDIO_FILE"
        exit 1
    fi

    echo "📤 发送语音消息测试..."
    echo "  文件: $AUDIO_FILE"

    # 检查 dws 配置
    if [[ -z "${DWS_PROFILE:-}" ]]; then
        echo "  ⚠️  未配置 DWS_PROFILE，跳过实际发送"
        exit 0
    fi

    # 确定发送目标（优先单聊，其次群聊）
    TARGET_CONV=""
    if [[ -n "${DWS_EVENT_O2O_USERS:-}" ]]; then
        # 取第一个单聊用户
        TARGET_USER="${DWS_EVENT_O2O_USERS%%,*}"
        echo "  目标: 单聊用户 $TARGET_USER"
        # TODO: 需要通过 dws 获取 openConversationId
        echo "  ⚠️  单聊发送需要手动实现，当前跳过"
    elif [[ -n "${DWS_EVENT_GROUP:-}" ]]; then
        TARGET_CONV="${DWS_EVENT_GROUP}"
        echo "  目标: 群聊 $TARGET_CONV"
        echo "  ⚠️  语音文件上传需要特殊处理，当前跳过"
    else
        echo "  ⚠️  未配置发送目标（DWS_EVENT_O2O_USERS 或 DWS_EVENT_GROUP）"
    fi

    echo
    echo "💡 手动测试步骤："
    echo "   1. 在钉钉中向数字员工发送一条语音消息"
    echo "   2. 检查日志: tail -f monitor.log | grep audio"
    echo "   3. 验证数字员工是否正确识别并回复"
else
    echo "💡 提示："
    echo "   组件检查已完成。要进行实际测试，请："
    echo "   1. 在钉钉中向数字员工发送一条语音消息"
    echo "   2. 检查日志: tail -f monitor.log | grep audio"
    echo "   3. 验证数字员工是否正确识别并回复"
fi

echo
echo "✅ 测试完成"
