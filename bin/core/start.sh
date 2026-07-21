#!/bin/bash
# start.sh — 启动数字员工服务
#
# 用途: 开发者手动启动服务，或被 reboot.sh 调用作为重启的第二步。
#
# 启动策略:
#   launchd 模式 — 若 agent 已加载则 kickstart -k 重启；否则 bootstrap 加载（优先）或 load -w
#   nohup 模式   — nohup monitor.sh --foreground 后台运行
#
# 用法:
#   bash bin/core/start.sh              # 自动判定模式（REBOOT_RESTART_MODE=auto）
#   REBOOT_RESTART_MODE=nohup bash bin/core/start.sh   # 强制 nohup 模式

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/bin/core/lib.sh"

COMPONENT_NAME="start"

# 加载常量
if [[ -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
elif [[ -f "$SCRIPT_DIR/config/constants.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.sh"
fi

: "${MONITOR_LOG:=$SCRIPT_DIR/monitor.log}"

notify_alert() {
    local msg="$1"
    log "告警: $msg"
    # 用户实现：发到机器人/邮件/Slack 等
    if declare -F notify_alert_handler >/dev/null 2>&1; then
        notify_alert_handler "$msg"
    fi
}

# macOS keychain 预检（dws 依赖 keychain 存储 token）
if [[ "$(uname)" == "Darwin" ]] && ! security unlock-keychain -u ~/Library/Keychains/login.keychain-db 2>/dev/null; then
    log "⚠️ macOS keychain 已锁定，dws 认证可能失败"
    log "   解决方法: 1) security unlock-keychain ~/Library/Keychains/login.keychain-db"
    log "            2) 或在 constants.local.sh 设置 DWS_DISABLE_KEYCHAIN=1"
fi

# 判定重启机制
_mode=$(resolve_restart_mode)
log "启动服务（模式: ${_mode}）..."

if [[ "$_mode" == "nohup" ]]; then
    # nohup 模式：后台启动 monitor（它会 start_all 拉起全部组件）
    log "nohup 模式：启动 monitor 进程..."
    nohup bash "$SCRIPT_DIR/bin/core/monitor.sh" --foreground \
        >> "$MONITOR_LOG" 2>&1 &
    disown 2>/dev/null || true
    log "monitor 已通过 nohup 启动，将拉起全部组件"
    exit 0
fi

# launchd 模式：优先 kickstart（agent 已加载）；否则 bootstrap/load 加载
_launchd_start() {
    local label_path="gui/$(id -u)/$LAUNCHD_LABEL"

    # agent 已加载？kickstart -k 重启
    if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
        log "agent 已加载，使用 kickstart -k 重启..."
        if launchctl kickstart -k "$label_path" 2>&1; then
            log "  kickstart 成功"
            return 0
        else
            log "  kickstart 失败"
            return 1
        fi
    fi

    # agent 未加载，bootstrap 加载（macOS 10.11+）
    log "agent 未加载，使用 bootstrap 加载..."
    if launchctl bootstrap "$label_path" "$LAUNCHD_PLIST" 2>&1; then
        log "  bootstrap 成功"
        return 0
    fi

    # bootstrap 失败，退回 load -w（旧版 macOS）
    log "  bootstrap 失败，退回 load -w..."
    if launchctl load -w "$LAUNCHD_PLIST" 2>&1; then
        log "  load -w 成功"
        return 0
    fi

    log "  load -w 失败"
    return 1
}

log "launchctl 启动 launchd agent ($LAUNCHD_LABEL)..."
if _launchd_start; then
    log "启动成功，monitor 会拉起全部组件"
    exit 0
fi

log "第一次启动失败，${KICKSTART_RETRY_INTERVAL}s 后重试..."
sleep "$KICKSTART_RETRY_INTERVAL"
if _launchd_start; then
    log "第二次启动成功"
    exit 0
fi

notify_alert "⚠️ launchd 启动失败两次，请人工介入：launchctl kickstart -k gui/$(id -u)/$LAUNCHD_LABEL"
exit 1  # 非零让 launchd 拉起（双保险）
