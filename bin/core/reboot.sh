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

: "${KICKSTART_RETRY_INTERVAL:=10}"
: "${LAUNCHD_LABEL:=com.example.agent-connect}"

notify_alert() {
    local msg="$1"
    log "告警: $msg"
    # 用户实现：发到机器人/邮件/Slack 等
    if declare -F notify_alert_handler >/dev/null 2>&1; then
        notify_alert_handler "$msg"
    fi
}

# 等待主进程退出（避免 pkill 刚派生的本脚本）
sleep 1

log "停止所有组件..."
pkill -f "agent-connect.*--unified-app-id" 2>/dev/null || true
pkill -f "agent-serve" 2>/dev/null || true
pkill -f "event-watcher\.py" 2>/dev/null || true
pkill -f "serve-watcher\.sh" 2>/dev/null || true

# 清状态文件
rm -f "$SCRIPT_DIR"/.{connect,serve,serve-watcher,event-watcher,monitor}.pid \
      "$SCRIPT_DIR"/.next-check \
      "$SCRIPT_DIR"/.serve.{port,pwd} 2>/dev/null || true

sleep 2

# launchctl kickstart 重启 launchd agent（带退避重试）
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
