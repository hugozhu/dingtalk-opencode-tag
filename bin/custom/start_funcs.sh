#!/bin/bash
# start_funcs.sh (custom) — FDE 在这里覆盖组件启动命令
#
# 被 bin/core/start_funcs.sh 在末尾 source，覆盖 core 的默认实现。
# 只需重定义你要定制的 start_* 函数；未定义的沿用 core 默认。
#
# 可用助手: _spawn <pid_file> <log_file> <cmd...>
# 可用变量: SCRIPT_DIR / CONNECT_LOG / MONITOR_LOG
#
# 约定的组件（见 monitor.sh 的 COMP_NAMES）: serve / connect / watcher / event_watcher

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
# start_watcher() {
#     _spawn "$SCRIPT_DIR/.watcher.pid" "${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}" \
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
# HARNESS_COMP_NAMES 顺序: serve connect watcher event_watcher
for _i in "${!COMP_NAMES[@]}"; do
    if [[ "${COMP_NAMES[$_i]}" == "connect" ]]; then
        COMP_PATTERNS[$_i]='dws-connect.sh'
        break
    fi
done

# start_connect — 拉起 dws-connect.sh（内部跑 dws event consume | bridge 管道）
start_connect() {
    _spawn "$SCRIPT_DIR/.connect.pid" "$CONNECT_LOG" \
        bash "$SCRIPT_DIR/bin/custom/dws-connect.sh"
}
