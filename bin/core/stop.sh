#!/bin/bash
# stop.sh — 停止数字员工服务
#
# 用途: 开发者手动停止服务，或被 reboot.sh 调用作为重启的第一步。
#
# 停止策略:
#   launchd 模式 — bootout 卸载 launchd agent（优先）或 unload，让服务保持停止状态
#   nohup 模式   — pkill monitor 进程
#   通用         — stop_components 杀所有组件（TERM→KILL）+ clean_runtime_state 清状态文件
#
# 用法:
#   bash bin/core/stop.sh              # 自动判定模式（REBOOT_RESTART_MODE=auto）
#   REBOOT_RESTART_MODE=nohup bash bin/core/stop.sh   # 强制 nohup 模式

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/bin/core/lib.sh"

COMPONENT_NAME="stop"

# 加载常量（env/PATH + REBOOT_RESTART_MODE / LAUNCHD_LABEL 等）
if [[ -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.local.sh"
elif [[ -f "$SCRIPT_DIR/config/constants.sh" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/config/constants.sh"
fi

# 设置组件配置（填充 COMP_NAMES/COMP_PATTERNS/COMP_PID_FILES + custom 覆盖）
# 需要 MONITOR_LOG/CONNECT_LOG 定义（start_funcs.sh 里 start_serve 等函数会引用，
# 虽然 stop 不调它们，但 source 时会定义函数体，bash 会校验变量绑定）
: "${MONITOR_LOG:=$SCRIPT_DIR/monitor.log}"
: "${CONNECT_LOG:=$SCRIPT_DIR/agent-connect.log}"
export MONITOR_LOG CONNECT_LOG
setup_components

# 判定重启机制
_mode=$(resolve_restart_mode)
log "停止服务（模式: ${_mode}）..."

# 1. 停止 supervisor（否则它会重新拉起组件）
if [[ "$_mode" == "launchd" ]]; then
    log "停止 launchd agent ($LAUNCHD_LABEL)..."
    # bootout 优先（macOS 10.11+），失败则退回 unload
    if launchctl bootout "gui/$(id -u)/$LAUNCHD_LABEL" 2>&1; then
        log "  bootout 成功"
    elif launchctl unload "$LAUNCHD_PLIST" 2>&1; then
        log "  unload 成功（bootout 不可用或失败）"
    else
        log "  ⚠️ bootout/unload 都失败，跳过（agent 可能未加载）"
    fi
else
    # nohup 模式：杀掉 monitor 进程（stop.sh 自己是被 reboot 以 start_new_session 派生，
    # 或被开发者手动调，不在 monitor 进程组里，pkill 不会误杀自己）
    # 不只匹配 "--foreground"：手工 `bash bin/core/monitor.sh`（无参数，同样进前台循环）
    # 起的 monitor 也要杀，否则它带着旧 env 继续兜底拉起组件（#71 reboot 后 env 不生效）。
    # 按「bash + 路径」双条件过滤，避免 pkill -f 误杀 cmdline 恰好含该路径的编辑器等进程。
    log "停止 monitor 进程..."
    for _mpid in $(pgrep -f "bin/core/monitor.sh" 2>/dev/null); do
        case "$(ps -p "$_mpid" -o command= 2>/dev/null)" in
            *bash*bin/core/monitor.sh*) kill -9 "$_mpid" 2>/dev/null || true ;;
        esac
    done
fi

# 2. 停止所有组件（含子进程树）；custom 可定义 stop_extra_cleanup 钩子做额外清扫
#    （如 dws event _bus 孤儿——进程树断裂后 PID 文件 / COMP_PATTERNS 都够不着，#71）
log "停止所有组件..."
stop_components TERM
if declare -F stop_extra_cleanup >/dev/null 2>&1; then stop_extra_cleanup TERM; fi
sleep 2
stop_components KILL   # SIGKILL 兜底
if declare -F stop_extra_cleanup >/dev/null 2>&1; then stop_extra_cleanup KILL; fi

# 3. 清理运行时状态
log "清理运行时状态..."
clean_runtime_state

log "服务已停止"
exit 0
