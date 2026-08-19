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

# run_with_timeout <secs> <cmd...>：带硬超时地跑一条命令。
# 返回被包裹命令的退出码；超时则杀掉整棵进程树并返回 124（对齐 GNU timeout(1) 的约定）。
#
# 为什么自己写：macOS 没有 timeout(1)（那是 GNU coreutils），而本仓库的守护脚本必须
# 在 stock macOS /bin/bash 3.2 下跑。故只用后台作业 + 轮询实现，不依赖任何外部工具。
# 用 kill_tree 而非 kill：被包裹的命令可能自身是管道/脚本（如 healthcheck 里的 curl），
# 只杀父进程会留下孤儿继续占着资源。
run_with_timeout() {
    local secs="$1"; shift
    "$@" &
    local cmd_pid=$!
    local waited=0
    while [[ "$waited" -lt "$secs" ]]; do
        kill -0 "$cmd_pid" 2>/dev/null || break
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$cmd_pid" 2>/dev/null; then
        kill_tree "$cmd_pid" KILL
        wait "$cmd_pid" 2>/dev/null || true
        return 124
    fi
    wait "$cmd_pid"
}

# stat 可移植取值 — stat_field <gnu_fmt> <bsd_fmt> <file>，失败/异常一律回 0。
#
# **GNU 必须先试**：GNU stat 的 -f 是「查文件系统信息」而非格式化，`stat -f %i file`
# 会把 %i 当文件名报错(rc=1)，却仍把 file 的文件系统报告（File:/ID:/Namelen:…）打到
# stdout。命令替换先收 stdout，`||` 回退救不回来，调用方拿到整段多行文本：
#   - 进 $(( )) → "File: unbound variable"（set -u 下直接中止脚本）
#   - 当 inode 存进 offset 文件 → 比对恒不等，窗口计数每次从头算
# 末尾 tr -dc 是二次兜底，把任何平台泄漏的非数字噪声在返回前剥干净。
stat_field() {
    local gnu_fmt="$1" bsd_fmt="$2" file="$3" val=""
    val=$(stat -c "$gnu_fmt" "$file" 2>/dev/null || stat -f "$bsd_fmt" "$file" 2>/dev/null || echo 0)
    val=$(printf '%s' "$val" | tr -dc '0-9')
    printf '%s' "${val:-0}"
}

# count_new_matches <log_file> <offset_file> <grep_pattern> <consume>
#
# 统计 <log_file> 里**距上次调用以来**新增的、匹配 <grep_pattern> 的行数。
# 输出一行：`<inode> <size> <count>`。consume 非空时把 `<inode> <size>` 写回
# <offset_file>，作为下次的窗口起点；为空则只看不动（peek）。
#
# 为什么按字节偏移而不是时间戳：目标日志（opencode.log）在 AGENT_DEBUG=1 下混有大量
# 多行 REQ/RESP body，按时间戳切窗要逐行解析；按偏移只需 tail -c，且天然不受
# body 里的时间戳文本干扰。
#
# **首次调用（无 offset 文件）只建基线、计数 0** —— 不倒算历史。这条很关键：
# 服务重启后 clean_runtime_state 会删掉 offset 文件，若此时把积压的历史失败全算进来，
# 一启动就会误判成"刚刚坏了"。
# 轮转（inode 变）或截断（size < 记录的 offset）→ 从新文件头开始算。
count_new_matches() {
    local log_file="$1" offset_file="$2" pattern="$3" consume="${4:-}"
    local off=0 saved_ino="" cur_ino=0 size=0 n=0

    if [[ ! -f "$log_file" ]]; then
        echo "0 0 0"
        return
    fi
    size=$(wc -c < "$log_file" 2>/dev/null | tr -d ' ')
    [[ "$size" =~ ^[0-9]+$ ]] || size=0
    cur_ino=$(stat_field %i %i "$log_file")

    if [[ ! -f "$offset_file" ]]; then
        [[ -n "$consume" ]] && echo "$cur_ino $size" > "$offset_file"
        echo "$cur_ino $size 0"
        return
    fi

    read -r saved_ino off < "$offset_file" 2>/dev/null || true
    [[ "$off" =~ ^[0-9]+$ ]] || off=0
    if [[ "$saved_ino" != "$cur_ino" || "$size" -lt "$off" ]]; then
        off=0
    fi

    # grep -c 零匹配时退出码为 1，set -e 下会杀掉整个脚本 —— 必须 || true 且兜默认值
    n=$(tail -c "+$((off + 1))" "$log_file" 2>/dev/null | grep -c -E "$pattern" 2>/dev/null || true)
    [[ "$n" =~ ^[0-9]+$ ]] || n=0

    [[ -n "$consume" ]] && echo "$cur_ino $size" > "$offset_file"
    echo "$cur_ino $size $n"
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
# .opencode-log.offset 必须在这张表里：stop/reboot 时删掉它，下次启动才会以当前日志大小
# 重新建基线；否则重启后会把停机前积压的失败当成"刚刚新增的"，一起步就误判为大脑坏了。
# .ext-sessions 同理 —— 探针崩溃留下的残留 sid 要能被清掉。
HARNESS_EXTRA_STATE_BASENAMES=(".next-check" ".serve.port" ".serve.pwd" ".opencode-connect-status.json" ".opencode-log.offset" ".ext-sessions")

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
