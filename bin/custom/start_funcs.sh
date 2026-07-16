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
