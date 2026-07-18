#!/bin/bash
# reboot.sh — 远程重启指令执行体
#
# 提炼自: dingtalk-opencode-agent/reboot.sh (v4.1)
# 原作者: hugozhu
#
# 用途: 用户通过聊天发 /reboot 指令时，主进程派生本脚本（脱离进程组）+ os._exit(0)，
# 本脚本 1s 后 pkill 四组件 + 清状态 + launchctl kickstart 重启 launchd agent。
#
# 失败传播 (v4.1):
#   - launchctl kickstart 失败 → 退避 KICKSTART_RETRY_INTERVAL 重试一次
#   - 仍失败 → notify_alert 发告警 + exit 1（非零让 launchd 拉起，双保险）
#   - 旧实现仅记 rc、脚本仍 exit 0，组件全死时无人知晓

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/bin/core/lib.sh"

COMPONENT_NAME="reboot"

# 加载常量（env/PATH + 可选覆盖如 REBOOT_RESTART_MODE），与 monitor.sh 一致
if [[ -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
elif [[ -f "$SCRIPT_DIR/config/constants.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.sh"
fi

: "${KICKSTART_RETRY_INTERVAL:=10}"
: "${LAUNCHD_LABEL:=com.example.agent-connect}"
# 重启机制: launchd(launchctl kickstart) | nohup(直接重启 monitor 进程) | auto(自动判定)。
# auto: launchd agent 已加载 → launchd，否则 → nohup。nohup 部署（monitor 由 nohup 拉起、
# 非 launchd 托管）必须走 nohup，否则 kickstart 找不到 label、组件停了没人重启。
: "${REBOOT_RESTART_MODE:=auto}"

notify_alert() {
    local msg="$1"
    log "告警: $msg"
    # 用户实现：发到机器人/邮件/Slack 等
    if declare -F notify_alert_handler >/dev/null 2>&1; then
        notify_alert_handler "$msg"
    fi
}

# 组件配置：与 monitor.sh 一致 —— 从 lib.sh 派生 + 通过 start_funcs.sh 应用 custom 的
# COMP_PATTERNS 覆盖（如 serve→'opencode serve'、connect→'dws-connect.sh'）。
# 只用默认 HARNESS_COMP_PATTERNS 会匹配不到自定义进程 → 组件杀不掉。
COMP_NAMES=("${HARNESS_COMP_NAMES[@]}")
COMP_PATTERNS=("${HARNESS_COMP_PATTERNS[@]}")
COMP_PID_FILES=()
for _b in "${HARNESS_COMP_PID_BASENAMES[@]}"; do
    COMP_PID_FILES+=("$SCRIPT_DIR/$_b")
done
# shellcheck disable=SC1091
source "$SCRIPT_DIR/bin/core/start_funcs.sh"

# stop_components <signal>：按 PID 文件（权威）+ cmdline 模式（覆盖后）双路杀，连子进程。
# PID 文件路杀掉 monitor 记录的确切进程；模式路兜底 PID 文件丢失/孤儿子进程。
stop_components() {
    local sig="$1" pf pid pat
    for pf in "${COMP_PID_FILES[@]}"; do
        [[ -f "$pf" ]] || continue
        pid=$(cat "$pf" 2>/dev/null)
        [[ -n "$pid" ]] && kill_tree "$pid" "$sig"
    done
    for pat in "${COMP_PATTERNS[@]}"; do
        for pid in $(pgrep -f "$pat" 2>/dev/null); do
            kill_tree "$pid" "$sig"
        done
    done
}

# 等待主进程退出（避免 pkill 刚派生的本脚本）
sleep 1

log "停止所有组件..."
stop_components TERM
sleep 2
stop_components KILL   # SIGKILL 兜底：TERM 没死透的强杀，确保重启前组件干净

# 判定重启机制：auto 时 launchd agent 已加载 → launchd，否则 → nohup
_mode="$REBOOT_RESTART_MODE"
if [[ "$_mode" == "auto" ]]; then
    if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
        _mode="launchd"
    else
        _mode="nohup"
    fi
fi
log "重启机制: $_mode"

if [[ "$_mode" == "nohup" ]]; then
    # nohup 模式：本脚本自己重启 monitor 进程（无 launchd 可 kickstart）。
    # 本脚本由 event_watcher 以 start_new_session 派生，已脱离 monitor 进程组，
    # 故 SIGKILL 旧 monitor 不会误杀自己。SIGKILL 避免旧 monitor cleanup 的
    # release_lock 与新 monitor 抢锁竞争（锁文件下面显式清）。
    log "nohup 模式：重启 monitor 进程..."
    pkill -9 -f "monitor.sh --foreground" 2>/dev/null || true
    sleep 1
    # 清状态（组件 PID + 锁 + 额外运行时状态）
    rm -f "$HARNESS_MONITOR_LOCK" 2>/dev/null || true
    for _b in "${HARNESS_COMP_PID_BASENAMES[@]}" "${HARNESS_EXTRA_STATE_BASENAMES[@]}"; do
        rm -f "$SCRIPT_DIR/$_b" 2>/dev/null || true
    done
    # 重新拉起 monitor（它 start_all 拉起全部组件）。monitor.sh 自己会 source
    # constants.local.sh（含 PATH/env），故这里无需额外准备环境。
    nohup bash "$SCRIPT_DIR/bin/core/monitor.sh" --foreground \
        >> "${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}" 2>&1 &
    disown 2>/dev/null || true
    log "monitor 已通过 nohup 重启，将拉起全部组件"
    exit 0
fi

# launchd 模式：清状态 + launchctl kickstart 重启 launchd agent（带退避重试）
rm -f "$HARNESS_MONITOR_LOCK" 2>/dev/null || true
for _b in "${HARNESS_COMP_PID_BASENAMES[@]}" "${HARNESS_EXTRA_STATE_BASENAMES[@]}"; do
    rm -f "$SCRIPT_DIR/$_b" 2>/dev/null || true
done

sleep 2

log "launchctl kickstart 重启 launchd agent ($LAUNCHD_LABEL)..."
if launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" 2>&1; then
    log "kickstart 成功，主进程会重新拉起全部组件"
    exit 0
fi

log "kickstart 第一次失败，${KICKSTART_RETRY_INTERVAL}s 后重试..."
sleep "$KICKSTART_RETRY_INTERVAL"
if launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" 2>&1; then
    log "kickstart 第二次成功"
    exit 0
fi

notify_alert "⚠️ launchctl kickstart 重启失败两次，请人工介入：launchctl kickstart -k gui/$(id -u)/$LAUNCHD_LABEL"
exit 1  # 非零让 launchd 拉起（双保险）
