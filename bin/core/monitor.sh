#!/bin/bash
# monitor.sh — launchd 托管的守护进程模板
#
# 提炼自: dingtalk-opencode-agent/monitor.sh (v4.1)
# 原作者: hugozhu
#
# 核心职责:
#   1. cleanup_stale_state: 启动时清理失效的 PID 文件（PID 死/被复用 → 删除）
#   2. start_all: 拉起 N 个组件（nohup+disown，脱离进程树独立存活；已有同种进程则跳过）
#   3. warmup: 触发依赖服务首次启动 + 提取凭据
#   4. 主循环：每 N 分钟健康检查
#      - 健康   → 重置失败计数，兜底拉起子组件（30 分钟内自愈）
#      - 不健康 → 全量重启（kill all + rm state + 重拉）
#      - 连续 N 次失败 → 熔断：发告警 + exit 0（等人工）
#   5. SIGTERM/SIGINT → cleanup exit 1（非零让 launchd 拉起，覆盖系统重启场景）
#
# 启动方式:
#   --foreground: 前台跑守护循环（launchd 调用）
#   --check:      单次健康检查+重启（不进循环，调试用）
#   (无参数):     nohup 后台启动（不推荐日常使用）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$SCRIPT_DIR/bin/core/lib.sh"

COMPONENT_NAME="monitor"

# 加载可配置常量（真实值在 config/constants.local.sh，被 .gitignore 忽略）
if [[ -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    source "$SCRIPT_DIR/config/constants.local.sh"
elif [[ -f "$SCRIPT_DIR/config/constants.sh" ]]; then
    source "$SCRIPT_DIR/config/constants.sh"
fi

# 可配置常量（被 config/constants.sh 覆盖）
: "${CHECK_INTERVAL:=1800}"           # 健康检查间隔（秒）
: "${MAX_FAILURES:=3}"               # 连续失败熔断阈值
: "${WARMUP_TIMEOUT:=60}"            # warmup 超时
: "${LOCK_FILE:=/tmp/agent-monitor.lock}"

# 组件配置：每个组件 = PID 文件 + cmdline 签名 + 启动函数
# 数组下标约定：COMP_NAMES / COMP_PID_FILES / COMP_PATTERNS
# 注意：COMP_NAMES 用下划线（bash 函数名不能含连字符），对应 start_<name> 函数
COMP_NAMES=("connect" "watcher" "event_watcher")
COMP_PID_FILES=("$SCRIPT_DIR/.connect.pid" "$SCRIPT_DIR/.watcher.pid" "$SCRIPT_DIR/.event-watcher.pid")
COMP_PATTERNS=("agent-connect.*--unified-app-id" "serve-watcher\.sh" "event-watcher\.py")

# 加载组件启动函数（core 默认 + custom 覆盖）。定义 start_connect / start_watcher /
# start_event_watcher，被 start_all / 兜底拉起调用。
source "$SCRIPT_DIR/bin/core/start_funcs.sh"

# 进程检测：是否在运行
is_running() {
    local idx="$1"
    verify_pid "${COMP_PID_FILES[$idx]}" "${COMP_PATTERNS[$idx]}"
}

# cleanup_stale_state: 启动时对每个组件 PID 文件做失效检测
cleanup_stale_state() {
    log "清理可能失效的状态文件..."
    for i in "${!COMP_NAMES[@]}"; do
        _cleanup_pidfile "${COMP_PID_FILES[$i]}" "${COMP_NAMES[$i]}" "${COMP_PATTERNS[$i]}"
    done
}

# start_all: 拉起所有组件（带去重）
start_all() {
    log "启动全部组件..."
    for i in "${!COMP_NAMES[@]}"; do
        if is_running "$i"; then
            log "  ${COMP_NAMES[$i]} 已在运行，跳过启动"
            continue
        fi
        log "  ${COMP_NAMES[$i]} 未运行，拉起..."
        # 调用对应启动函数（需用户在 start_funcs.sh 里实现）
        # start_<name>() 函数约定: nohup + disown + echo $! > PID 文件
        "start_${COMP_NAMES[$i]}"
    done
}

# stop_all: 杀掉所有组件
stop_all() {
    log "停止残留进程..."
    for pattern in "${COMP_PATTERNS[@]}"; do
        pkill -f "$pattern" 2>/dev/null || true
    done
    sleep 2
    for f in "${COMP_PID_FILES[@]}"; do
        rm -f "$f"
    done
}

# warmup: 触发依赖服务首次启动 + 提取凭据
# 用户实现 warmup_serve() 函数
warmup() {
    log "warmup: 触发依赖服务首次启动..."
    if declare -F warmup_serve >/dev/null 2>&1; then
        warmup_serve
    fi
}

# 健康检查（用户实现 do_healthcheck 返回 0/非0）
run_healthcheck() {
    bash "$SCRIPT_DIR/bin/core/healthcheck.sh"
}

# 熔断告警（用户实现 notify_alert <msg>）
notify_alert() {
    local msg="$1"
    log "熔断告警: $msg"
    if declare -F notify_alert_handler >/dev/null 2>&1; then
        notify_alert_handler "$msg"
    fi
}

# cleanup: SIGTERM/SIGINT 时清理（释放锁 + exit 1）
cleanup() {
    release_lock "$LOCK_FILE"
    log "monitor cleanup 退出"
    exit 1  # 非零让 launchd 拉起
}
trap cleanup SIGTERM SIGINT

# 主循环
run_forever() {
    local fail_count=0
    while true; do
        sleep "$CHECK_INTERVAL"
        if run_healthcheck; then
            log "健康检查通过"
            fail_count=0
            # 兜底拉起：watcher 死亡 30 分钟内自愈
            for i in "${!COMP_NAMES[@]}"; do
                if ! is_running "$i"; then
                    log "${COMP_NAMES[$i]} 死亡，兜底拉起"
                    "start_${COMP_NAMES[$i]}"
                fi
            done
        else
            fail_count=$((fail_count + 1))
            log "健康检查失败 (连续 $fail_count 次)"
            if [[ "$fail_count" -ge "$MAX_FAILURES" ]]; then
                notify_alert "连续 $MAX_FAILURES 次健康检查失败，已停止自动重启，请人工介入"
                exit 0  # exit 0 = 成功退出，launchd 不再拉起，等人工
            fi
            stop_all
            sleep 3
            start_all
            sleep 3
            # 复查一次
            if run_healthcheck; then
                fail_count=0
                log "重启后健康检查通过"
            fi
        fi
    done
}

# --check 模式：单次健康检查+重启（不进循环）
do_check() {
    if run_healthcheck; then
        log "健康检查通过"
        exit 0
    fi
    log "健康检查失败，全量重启"
    stop_all
    start_all
    warmup
    sleep 3
    if run_healthcheck; then
        exit 0
    fi
    exit 1
}

# main
main() {
    if ! acquire_lock "$LOCK_FILE"; then
        log "已有 monitor 实例在跑（PID=$(cat "$LOCK_FILE")），退出"
        exit 0
    fi
    log "监控开始 (pid=$$, interval=${CHECK_INTERVAL}s, 由 launchd 托管)"
    cleanup_stale_state
    trap cleanup SIGTERM SIGINT

    start_all
    sleep 3
    warmup
    bash "$SCRIPT_DIR/bin/core/healthcheck.sh"
    date -v+${CHECK_INTERVAL}M '+%s' > "$SCRIPT_DIR/.next-check"

    case "${1:---foreground}" in
        --foreground) run_forever ;;
        --check)      do_check ;;
        *)            log "未知参数 $1"; exit 2 ;;
    esac
}

main "$@"
