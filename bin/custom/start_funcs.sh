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

# ---------------------------------------------------------------------------
# serve 组件：托管 opencode serve
#   - brain(opencode) 走 HTTP 生成回复（POST /session/{id}/message），省 CLI 冷启动
#   - 合并转发业务路径也用它（agent_common.inject_and_forward）
# healthcheck 对 serve 硬失败，必须写出 .serve.pid / .serve.port / .serve.pwd。
# 端口可用 config/constants.local.sh 的 OPENCODE_SERVE_PORT 覆盖；密码优先复用已存在
# 的 .serve.pwd（重启 serve 时保持凭据稳定，避免 in-flight 请求 401）。
#
# AGENT_DEBUG 开启时：给 opencode serve 加 --print-logs --log-level（默认 DEBUG），
# 把 serve 自身日志打到独立文件 serve.log（默认 $SCRIPT_DIR/serve.log，可用
# AGENT_SERVE_LOG 覆盖），便于排查 serve 侧问题；不污染 monitor.log。级别可用
# AGENT_SERVE_LOG_LEVEL 覆盖（DEBUG|INFO|WARN|ERROR）。关闭时 serve 静默，日志入 monitor.log。
# ---------------------------------------------------------------------------
start_serve() {
    local port="${OPENCODE_SERVE_PORT:-4096}"
    local pwd_file="$SCRIPT_DIR/.serve.pwd"
    local pw
    # || true：文件不存在时 cat 返回非零，set -e 下会杀掉 monitor（冷启动/reboot 清了
    # .serve.pwd 后就没这文件）。吞掉失败，下面按空值重新生成。
    pw="$(cat "$pwd_file" 2>/dev/null || true)"
    [[ -z "$pw" ]] && pw="$(openssl rand -hex 16)"
    echo "$port" > "$SCRIPT_DIR/.serve.port"
    echo "$pw"   > "$pwd_file"

    # serve 启动参数（AGENT_DEBUG 时加日志开关）
    local serve_args=(serve --port "$port" --hostname 127.0.0.1)
    local serve_log="${MONITOR_LOG:-$SCRIPT_DIR/monitor.log}"
    case "$(printf '%s' "${AGENT_DEBUG:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on)
            serve_args+=(--print-logs --log-level "${AGENT_SERVE_LOG_LEVEL:-DEBUG}")
            serve_log="${AGENT_SERVE_LOG:-$SCRIPT_DIR/serve.log}"
            ;;
    esac

    # OPENCODE_SERVER_PASSWORD 让 serve 要求 Basic auth(opencode:<pwd>)，与
    # healthcheck check_serve_http / agent_common.find_serve_credentials 约定一致。
    OPENCODE_SERVER_PASSWORD="$pw" _spawn "$SCRIPT_DIR/.serve.pid" \
        "$serve_log" \
        "${AGENT_OPENCODE_BIN:-opencode}" "${serve_args[@]}"
}

# event_watcher 通常沿用 core 默认实现，无需覆盖。

# ---------------------------------------------------------------------------
# connect 组件：dws event consume（订阅指定群消息）→ bridge → CONNECT_LOG
# 群 conversationId / profile 从环境变量读取（见 config/constants.local.sh，gitignored）
# ---------------------------------------------------------------------------

# connect 进程的 cmdline 签名改为 dws-connect.sh（默认模式 agent-connect 匹配不到本实现）。
# serve 进程签名是 `opencode serve`（默认模式 agent-serve 也匹配不到），一并覆盖，
# 否则 verify_pid 认定 serve 死亡 → monitor 反复兜底拉起 → 熔断循环。
# 注意：verify_pid 的 cmdline 校验是**字面子串**匹配，模式里不要写正则转义 '\.'
# （'\.' 会当字面反斜杠，永远匹配不到）。'.' 作字面子串即可，pgrep 兜底也仍匹配。
# COMP_PATTERNS 在 monitor.sh 里已按下标赋值。
# HARNESS_COMP_NAMES 顺序: serve(0) connect(1) watcher(2) event_watcher(3)
for _i in "${!COMP_NAMES[@]}"; do
    case "${COMP_NAMES[$_i]}" in
        serve)   COMP_PATTERNS[$_i]='opencode serve' ;;
        connect) COMP_PATTERNS[$_i]='dws-connect.sh' ;;
    esac
done

# start_connect — 拉起 dws-connect.sh（内部跑 dws event consume | bridge 管道）
# CONNECT_LOG 兜底默认：monitor.sh 未导出该变量，冷启动时 set -u 会因未绑定变量崩溃
# （dws-connect.sh 内部也有同样兜底，这里补上 spawn 阶段的）。
start_connect() {
    _spawn "$SCRIPT_DIR/.connect.pid" "${CONNECT_LOG:-$SCRIPT_DIR/agent-connect.log}" \
        bash "$SCRIPT_DIR/bin/custom/dws-connect.sh"
}
