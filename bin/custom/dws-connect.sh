#!/bin/bash
# dws-connect.sh — connect 组件包装：dws event consume → bridge → CONNECT_LOG
#
# 由 start_connect（bin/custom/start_funcs.sh）拉起。作为独立命名脚本存在，使进程
# cmdline 签名稳定可被 verify_pid 匹配（模式 'dws-connect.sh'），且能承载管道
# （_spawn 只能跑单条命令，管道需要包在脚本里）。
#
# 订阅指定群/单聊消息，转成 connect-log 格式喂给 event_watcher 的 log-tail。
# **敏感值（群 conversationId、profile）不写死在本文件**，从环境变量读取，
# 真实值放在 gitignored 的 config/constants.local.sh：
#   DWS_EVENT_KEY    事件类型（默认群消息）
#   DWS_EVENT_GROUP  群 openConversationId（群消息必填）—— 敏感，不提交
#   DWS_PROFILE      组织 profile —— 敏感，不提交

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# 加载本地敏感常量（gitignored）。start_connect 经 monitor 已 source 过，这里兜底再 source
# 一次，便于直接手工运行本脚本调试。
if [[ -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
fi

: "${DWS_EVENT_KEY:=user_im_message_receive_group}"
: "${DWS_EVENT_GROUP:=}"
: "${DWS_PROFILE:=}"
: "${CONNECT_LOG:=$SCRIPT_DIR/agent-connect.log}"

if [[ -z "$DWS_PROFILE" ]]; then
    echo "[connect] ERROR: DWS_PROFILE 未设置（请在 config/constants.local.sh 填）" >> "$CONNECT_LOG"
    exit 1
fi
if [[ "$DWS_EVENT_KEY" == *group* && -z "$DWS_EVENT_GROUP" ]]; then
    echo "[connect] ERROR: 群订阅需要 DWS_EVENT_GROUP（请在 config/constants.local.sh 填）" >> "$CONNECT_LOG"
    exit 1
fi

BRIDGE="$SCRIPT_DIR/bin/custom/dws_event_bridge.py"

# 组装事件订阅参数（群消息需要 --group）
consume_args=(event consume "$DWS_EVENT_KEY" --profile "$DWS_PROFILE" -f ndjson --quiet)
if [[ "$DWS_EVENT_KEY" == *group* ]]; then
    consume_args+=(--group "$DWS_EVENT_GROUP")
fi

# 不把敏感 group/profile 打进日志（只记事件类型）
echo "[connect] dws-connect 启动: event=$DWS_EVENT_KEY" >> "$CONNECT_LOG"

# dws event consume 的 stderr（[event] ready / 状态）汇入 CONNECT_LOG 便于健康检查看活跃度
# bridge 的 stdout（connect-log 行）追加到 CONNECT_LOG，stderr（诊断）也进同一文件
exec dws "${consume_args[@]}" 2>>"$CONNECT_LOG" \
    | python3 "$BRIDGE" >> "$CONNECT_LOG" 2>>"$CONNECT_LOG"
