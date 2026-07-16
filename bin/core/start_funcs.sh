#!/bin/bash
# start_funcs.sh — 组件启动函数契约（被 monitor.sh source）
#
# monitor.sh 的 start_all / 兜底拉起会调用 start_<component>（见 COMP_NAMES）。
# 本文件提供**默认实现**，FDE 通过 bin/custom/start_funcs.sh **覆盖**业务特定的启动命令，
# 不改本文件（core）。
#
# 约定（每个 start_* 函数必须做到）:
#   1. nohup <cmd> >>"$log" 2>&1 &   # 脱离控制终端
#   2. echo $! > "<pid_file>"        # 写 PID 文件（供 verify_pid 检测）
#   3. disown                        # 脱离 monitor 进程树，monitor 退出后仍存活
#
# 变量来自 monitor.sh: SCRIPT_DIR / COMP_PID_FILES / CONNECT_LOG 等。

# _spawn <pid_file> <log_file> <cmd...> — 通用拉起助手（nohup + disown + 写 pid）
_spawn() {
    local pid_file="$1"; shift
    local log_file="$1"; shift
    nohup "$@" >>"$log_file" 2>&1 &
    local pid=$!
    echo "$pid" > "$pid_file"
    disown "$pid" 2>/dev/null || true
    log "  spawned pid=$pid → $pid_file (cmd: $1)"
}

# start_connect — 数字员工核心连接进程（**业务特定，FDE 在 custom 覆盖**）
# 默认实现仅告警：没有 connect 命令，harness 跑不起来。
start_connect() {
    log "  ⚠️ start_connect 未被 custom 覆盖 —— 请在 bin/custom/start_funcs.sh 实现"
    log "     示例: _spawn \"\$SCRIPT_DIR/.connect.pid\" \"\$CONNECT_LOG\" your-connect-cmd --flag ..."
    return 1
}

# start_watcher — serve 日志监控（**业务特定，FDE 在 custom 覆盖**）
# 默认实现为空跳过（serve-watcher 是可选组件）。
start_watcher() {
    log "  start_watcher 使用默认空实现（如需 serve-watcher，请在 custom 覆盖）"
    return 0
}

# start_event_watcher — SSE 事件流 + log-tail 主进程（**通用，core 提供默认实现**）
start_event_watcher() {
    local log_file="${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}"
    _spawn "$SCRIPT_DIR/.event-watcher.pid" "$log_file" \
        python3 "$SCRIPT_DIR/src/core/event_watcher.py"
}

# 加载 FDE 覆盖（存在则 source，覆盖上面的默认实现）
_CUSTOM_START_FUNCS="$SCRIPT_DIR/bin/custom/start_funcs.sh"
if [[ -f "$_CUSTOM_START_FUNCS" ]]; then
    # shellcheck disable=SC1090
    source "$_CUSTOM_START_FUNCS"
    log "已加载 custom 启动函数覆盖: $_CUSTOM_START_FUNCS"
fi
