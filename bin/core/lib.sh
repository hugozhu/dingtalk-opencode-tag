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

# acquire_lock <lock_file>：单实例锁（跨平台：文件存在性 + kill -0 检测，无外部依赖）
acquire_lock() {
    local lock_file="$1"
    # 用文件存在性 + 进程存活判断（最简、跨 macOS/Linux，无需 flock/shlock）
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

# kill_tree <pid> [signal]：先递归杀子进程再杀自己（子在前，避免留孤儿）。
# connect 是 `dws-connect.sh` → `dws event consume | python3 bridge` 管道，只按父脚本
# 模式 pkill 会把管道子进程（dws consume / bridge）甩成孤儿继续消费消息。默认 SIGTERM。
kill_tree() {
    local pid="$1"
    local sig="${2:-TERM}"
    local child
    for child in $(pgrep -P "$pid" 2>/dev/null); do
        kill_tree "$child" "$sig"
    done
    kill "-$sig" "$pid" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 组件清单单一真相源 — monitor.sh / reboot.sh / healthcheck.sh 共享，避免命名漂移
#   COMP_NAMES：组件名（下划线，对应 start_<name> 函数）
#   COMP_PID_BASENAMES：对应 PID 文件名（相对 SCRIPT_DIR）
#   COMP_PATTERNS：cmdline 签名（verify_pid / pkill 用）
# 顺序一一对应。改这里三个脚本同步生效。
# 注：移除 watcher（serve-watcher 可选组件，默认不使用）避免无意义的"死亡"日志
# ---------------------------------------------------------------------------
HARNESS_COMP_NAMES=("serve" "connect" "event_watcher")
HARNESS_COMP_PID_BASENAMES=(".serve.pid" ".connect.pid" ".event-watcher.pid")
HARNESS_COMP_PATTERNS=("agent-serve" "agent-connect.*--unified-app-id" "event_watcher.py")

# monitor 自身的运行时状态文件（reboot 清理时用）
HARNESS_MONITOR_LOCK="${LOCK_FILE:-/tmp/agent-monitor.lock}"
HARNESS_EXTRA_STATE_BASENAMES=(".next-check" ".serve.port" ".serve.pwd" ".opencode-connect-status.json")

# ---------------------------------------------------------------------------
# 服务控制共享函数 — start.sh / stop.sh / reboot.sh 共享逻辑，避免重复
# ---------------------------------------------------------------------------

# 服务控制常量（被 config/constants.local.sh 覆盖）
: "${KICKSTART_RETRY_INTERVAL:=10}"
: "${LAUNCHD_LABEL:=com.example.agent-connect}"
: "${LAUNCHD_PLIST:=$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist}"
: "${REBOOT_RESTART_MODE:=nohup}"

# resolve_restart_mode — 解析重启机制：launchd | nohup
# auto 时根据 launchd agent 是否已加载自动判定
resolve_restart_mode() {
    local mode="$REBOOT_RESTART_MODE"
    if [[ "$mode" == "auto" ]]; then
        if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
            mode="launchd"
        else
            mode="nohup"
        fi
    fi
    echo "$mode"
}

# setup_components — 从 HARNESS_* 派生组件配置 + source start_funcs.sh 应用 custom 覆盖
# 填充 COMP_NAMES / COMP_PATTERNS / COMP_PID_FILES（需调用方先 source constants.local.sh）
# monitor.sh 有自己的数组初始化，不调此函数；stop/start/reboot 共享此函数避免重复
setup_components() {
    COMP_NAMES=("${HARNESS_COMP_NAMES[@]}")
    COMP_PATTERNS=("${HARNESS_COMP_PATTERNS[@]}")
    COMP_PID_FILES=()
    for _b in "${HARNESS_COMP_PID_BASENAMES[@]}"; do
        COMP_PID_FILES+=("$SCRIPT_DIR/$_b")
    done
    # source start_funcs.sh 让 custom 的 COMP_PATTERNS 覆盖生效（如 serve→'opencode serve'）
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/bin/core/start_funcs.sh"
}

# stop_components <signal> — 按 PID 文件 + cmdline 模式双路杀组件（含子进程树）
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

# clean_runtime_state — 清理组件 PID 文件 + 锁 + 额外运行时状态
clean_runtime_state() {
    rm -f "$HARNESS_MONITOR_LOCK" 2>/dev/null || true
    for _b in "${HARNESS_COMP_PID_BASENAMES[@]}" "${HARNESS_EXTRA_STATE_BASENAMES[@]}"; do
        rm -f "$SCRIPT_DIR/$_b" 2>/dev/null || true
    done
}
