#!/bin/bash
# start_funcs.sh (custom) — FDE 在这里覆盖组件启动命令
#
# 被 bin/core/start_funcs.sh 在末尾 source，覆盖 core 的默认实现。
# 只需重定义你要定制的 start_* 函数；未定义的沿用 core 默认。
#
# 可用助手: _spawn <pid_file> <log_file> <cmd...>
# 可用变量: SCRIPT_DIR / CONNECT_LOG / MONITOR_LOG
#
# 约定的组件（见 monitor.sh 的 COMP_NAMES）: serve / connect / serve_watcher / event_watcher

# 示例：opencode serve 进程（替换为你的真实命令）
# healthcheck 对 serve 硬失败，必须实现本函数，且写出 .serve.port / .serve.pwd。
# start_serve() {
#     local port=4096
#     local pwd="$(openssl rand -hex 16)"
#     echo "$port" > "$SCRIPT_DIR/.serve.port"
#     echo "$pwd"  > "$SCRIPT_DIR/.serve.pwd"
#     AGENT_SERVER_PASSWORD="$pwd" _spawn "$SCRIPT_DIR/.serve.pid" \
#         "${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}" \
#         agent-serve --port "$port"
# }

# 示例：数字员工核心连接进程（替换为你的真实命令）
# start_connect() {
#     _spawn "$SCRIPT_DIR/.connect.pid" "$CONNECT_LOG" \
#         dws dev connect --unified-app-id your-app-id --agent-workdir "$SCRIPT_DIR"
# }

# 示例：serve 日志监控（可选）
# start_serve_watcher() {
#     _spawn "$SCRIPT_DIR/.serve-watcher.pid" "${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}" \
#         bash "$SCRIPT_DIR/bin/custom/serve-watcher.sh"
# }

# event_watcher 通常沿用 core 默认实现，无需覆盖。

# ---------------------------------------------------------------------------
# connect 组件：dws event consume（订阅指定群消息）→ bridge → CONNECT_LOG
# 群 conversationId / profile 从环境变量读取（见 config/constants.local.sh，gitignored）
# ---------------------------------------------------------------------------

# connect 进程的 cmdline 签名改为 dws-connect.sh（默认模式 agent-connect 匹配不到本实现）。
# 注意：verify_pid 的 cmdline 校验是**字面子串**匹配，模式里不要写正则转义 '\.'
# （'\.' 会当字面反斜杠，永远匹配不到）。'.' 作字面子串即可，pgrep 兜底也仍匹配。
# COMP_PATTERNS 在 monitor.sh 里已按下标赋值，connect 是 index 1（serve=0）。
# HARNESS_COMP_NAMES 顺序: serve connect serve_watcher event_watcher
for _i in "${!COMP_NAMES[@]}"; do
    if [[ "${COMP_NAMES[$_i]}" == "connect" ]]; then
        COMP_PATTERNS[$_i]='dws-connect.sh'
        break
    fi
done

# start_connect — 拉起 dws-connect.sh（内部跑 dws event consume | bridge 管道）
# CONNECT_LOG 兜底：monitor.sh 是 set -u 且不定义 CONNECT_LOG，裸引用会 unbound→杀死
# monitor（2026-07-17 混沌测试实测：connect 死后 monitor 首次调 start_connect 即崩）。
# 默认值与 dws-connect.sh 内部的 `: "${CONNECT_LOG:=...}"` 保持一致。
start_connect() {
    _spawn "$SCRIPT_DIR/.connect.pid" "${CONNECT_LOG:-$SCRIPT_DIR/agent-connect.log}" \
        bash "$SCRIPT_DIR/bin/custom/dws-connect.sh"
}

# ---------------------------------------------------------------------------
# start_serve — opencode serve（SSE /event 源）。覆盖 core 的告警占位实现。
#
# 为什么必须实现：healthcheck 对 serve / serve_http 硬失败。若不实现，monitor 的
# stop_all→start_all 自愈路径会调 core 默认 start_serve（return 1），而 monitor.sh
# 是 set -e —— 非零返回会让 monitor 自己退出，整个守护链崩塌（2026-07-17 实测事故）。
# 逻辑与 start-digital-employee.sh 的 serve 块一致：生成随机密码 + 写 .serve.{port,pwd}
# 供 healthcheck check_serve_http 和 event_watcher find_serve_credentials 发现。
# ---------------------------------------------------------------------------
start_serve() {
    local port="${SERVE_PORT:-4096}"
    local pwd
    pwd="$(openssl rand -hex 16)"
    echo "$port" > "$SCRIPT_DIR/.serve.port"
    echo "$pwd"  > "$SCRIPT_DIR/.serve.pwd"
    chmod 600 "$SCRIPT_DIR/.serve.pwd"
    OPENCODE_SERVER_PASSWORD="$pwd" _spawn "$SCRIPT_DIR/.serve.pid" \
        "${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}" \
        opencode serve --port "$port" --hostname 127.0.0.1
}

# ---------------------------------------------------------------------------
# start_serve_watcher — serve-watcher（opencode serve 快速探活 + 秒级单独重拉）。
# 覆盖 core 的空实现。cmdline 含 "serve-watcher.sh"，命中 HARNESS_COMP_PATTERNS 的
# serve_watcher 项，故 monitor is_running 能匹配到（否则空实现下 monitor 每轮误判
# "serve_watcher 死亡"兜底拉起 → 刷屏，2026-07-17 实测）。补齐 monitor 5min 体检之间的
# serve 盲区。
# ---------------------------------------------------------------------------
start_serve_watcher() {
    _spawn "$SCRIPT_DIR/.serve-watcher.pid" "${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}" \
        bash "$SCRIPT_DIR/bin/custom/serve-watcher.sh"
}
