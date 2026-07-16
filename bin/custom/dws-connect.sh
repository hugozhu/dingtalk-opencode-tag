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

# 递归杀掉子孙进程（管道两端 dws/bridge 及其后代）。用 pgrep -P 遍历而非进程组 kill，
# 因为两种启动方式进程组不同：launcher 用 setsid（本脚本是组长），monitor 用 nohup
# （本脚本与 monitor 同组，杀组会误伤 monitor）。深度优先：先杀后代再杀该子进程，
# 避免子进程先死、后代 reparent 到 init 后 pgrep -P 找不到。
_kill_tree() {
    local sig="$1" parent="$2" child
    for child in $(pgrep -P "$parent" 2>/dev/null); do
        _kill_tree "$sig" "$child"
        kill "-$sig" "$child" 2>/dev/null || true
    done
}

# 收到 TERM/INT（pkill / systemd stop / launchctl / reboot.sh）→ 连带清理子进程再退出，
# 否则 dws event consume 会被留成孤儿继续消费群消息。
cleanup() {
    trap - TERM INT
    echo "[connect] 收到停止信号，清理 dws/bridge 子进程…" >> "$CONNECT_LOG"
    _kill_tree TERM $$
    sleep 1
    _kill_tree KILL $$   # 顽固者补刀
    exit 0
}
trap cleanup TERM INT

# dws event consume 的 stderr（[event] ready / 状态）汇入 CONNECT_LOG 便于健康检查看活跃度
# bridge 的 stdout（connect-log 行）追加到 CONNECT_LOG，stderr（诊断）也进同一文件
# 后台跑 + wait：前台管道会阻塞信号处理，只有 wait 被信号打断才能立即触发 cleanup。
# （不能用 exec：exec 替换本 shell 后 trap 失效，子进程照旧成孤儿。）
dws "${consume_args[@]}" 2>>"$CONNECT_LOG" \
    | python3 "$BRIDGE" >> "$CONNECT_LOG" 2>>"$CONNECT_LOG" &
wait $!
