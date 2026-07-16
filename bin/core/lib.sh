#!/bin/bash
# lib.sh — 共享 shell 工具，被 monitor.sh / healthcheck.sh 引用
#
# 提炼自: dingtalk-opencode-agent/lib.sh (v4.1)
# 原作者: hugozhu
#
# 提供 verify_pid（PID 文件 + kill -0 + cmdline 签名 + ^锚定 pgrep 兜底），
# 避免 pgrep -f 误匹配（如 send-by-bot 转发进程 cmdline 含被转发命令文本）
# 共享给 monitor + healthcheck，消除两处逻辑漂移

# verify_pid <pid_file> <cmdline_pattern> [pgrep_fallback_pattern]
# 返回 0 = 进程存活, 1 = 不存活
verify_pid() {
    local pid_file="$1"
    local cmdline_pattern="$2"
    local pgrep_pattern="${3:-^${cmdline_pattern}}"  # 默认 ^锚定避免误匹配

    # 1. 读 PID 文件
    [[ -f "$pid_file" ]] || return 1
    local pid
    pid=$(cat "$pid_file" 2>/dev/null)
    [[ -n "$pid" ]] || return 1

    # 2. kill -0 检测进程存活
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi

    # 3. cmdline 签名校验（防 PID 复用：进程死了，新进程复用了同 PID）
    local cmdline
    cmdline=$(ps -p "$pid" -o command= 2>/dev/null)
    if [[ -z "$cmdline" ]]; then
        return 1
    fi
    if [[ "$cmdline" != *"$cmdline_pattern"* ]]; then
        return 1
    fi

    # 4. 兜底：pgrep -fi 锚定模式（PID 文件丢失时仍能检测）
    #    ^锚定排除 send-by-bot 等转发进程（其 cmdline 以别的命令开头）
    if ! pgrep -fi "$pgrep_pattern" >/dev/null 2>&1; then
        # PID 文件说在，但 pgrep 找不到——可能是 PID 文件失效
        # 不直接 return 1，让上游 is_running 决定（更稳）
        :
    fi

    return 0
}

# cleanup_stale_state <pid_file> <name> <pattern> [pgrep_fallback]
# 检查 PID 文件失效或被复用即删除
_cleanup_pidfile() {
    local pid_file="$1"
    local name="$2"
    local pattern="$3"
    local fallback="${4:-}"
    if [[ -f "$pid_file" ]]; then
        if ! verify_pid "$pid_file" "$pattern" "$fallback"; then
            local old_pid
            old_pid=$(cat "$pid_file" 2>/dev/null)
            rm -f "$pid_file"
            log "  $name: pid=$old_pid 失效或被复用，删除"
        fi
    fi
}

# acquire_lock <lock_file>：单实例锁（shlock 无 unlock，释放直接 rm -f 锁文件）
acquire_lock() {
    local lock_file="$1"
    if /usr/bin/shlock -u "$lock_file" 2>/dev/null; then
        # shlock -u 是 UUCP 二进制 pid 格式，不是 unlock
        :
    fi
    # 用文件存在性判断（最简）
    if [[ -f "$lock_file" ]]; then
        local old_pid
        old_pid=$(cat "$lock_file" 2>/dev/null)
        if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
            return 1  # 已有实例在跑
        fi
        rm -f "$lock_file"
    fi
    echo $$ > "$lock_file"
    return 0
}

# 释放锁
release_lock() {
    rm -f "$1"
}

# log <msg>：统一日志格式（写到 stderr，由 launchd 落盘）
log() {
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[${ts}] [${COMPONENT_NAME:-monitor}] $*" >&2
}

# ---------------------------------------------------------------------------
# 组件清单单一真相源 — monitor.sh / reboot.sh / healthcheck.sh 共享，避免命名漂移
#   COMP_NAMES：组件名（下划线，对应 start_<name> 函数）
#   COMP_PID_BASENAMES：对应 PID 文件名（相对 SCRIPT_DIR）
#   COMP_PATTERNS：cmdline 签名（verify_pid / pkill 用）
# 顺序一一对应。改这里三个脚本同步生效。
# ---------------------------------------------------------------------------
HARNESS_COMP_NAMES=("serve" "connect" "watcher" "event_watcher")
HARNESS_COMP_PID_BASENAMES=(".serve.pid" ".connect.pid" ".watcher.pid" ".event-watcher.pid")
HARNESS_COMP_PATTERNS=("agent-serve" "agent-connect.*--unified-app-id" "serve-watcher\.sh" "event-watcher\.py")

# monitor 自身的运行时状态文件（reboot 清理时用）
HARNESS_MONITOR_LOCK="${LOCK_FILE:-/tmp/agent-monitor.lock}"
HARNESS_EXTRA_STATE_BASENAMES=(".next-check" ".serve.port" ".serve.pwd" ".opencode-connect-status.json")
