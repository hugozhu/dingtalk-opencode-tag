#!/bin/bash
# serve-watcher.sh — opencode serve 快速探活 + 秒级单独重拉（custom 层）
#
# 定位：monitor 每 CHECK_INTERVAL（默认 5min）才体检一次，中间 serve 挂了会有最长
# 5 分钟盲区（SSE 断、群里不回复）。本 watcher 每 SERVE_WATCH_INTERVAL 秒探一次 serve，
# 连续失败即**单独重拉 serve**（不走 monitor 的全量重启），把盲区从分钟级压到秒级。
#
# 与 monitor 防冲突：重拉走带存活检测的原子锁（mkdir）+ 锁内复检 + 冷却，避免与 monitor
# 的 5min 体检、或自身抖动同时重拉出两个 serve 抢 4096 端口。复用 start_serve（与
# start-digital-employee.sh / monitor 同一份 serve 启动逻辑，写 .serve.port/.serve.pwd）。
#
# 由 monitor 的 start_serve_watcher 托管（见 bin/custom/start_funcs.sh）；cmdline 含
# "serve-watcher.sh"，对应 HARNESS_COMP_PATTERNS 的 serve_watcher 项，故 monitor 能 is_running 到它。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/bin/core/lib.sh"
COMPONENT_NAME="serve-watch"

# 常量（真实值/覆盖在 config/constants.local.sh）
if [[ -f "$SCRIPT_DIR/config/constants.local.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/config/constants.local.sh"
fi

: "${SERVE_PORT:=4096}"
: "${SERVE_WATCH_INTERVAL:=20}"      # 探测间隔（秒）
: "${SERVE_WATCH_FAILS:=2}"          # 连续失败几次才重拉（防瞬时抖动）
: "${SERVE_WATCH_COOLDOWN:=60}"      # 重拉后冷却（秒），避免抖动风暴
: "${SERVE_RESTART_LOCK:=/tmp/agent-serve-restart.lock}"

# 复用统一的 start_serve（需 COMP_NAMES 供 custom start_funcs 顶部循环，同 monitor.sh）
COMP_NAMES=("${HARNESS_COMP_NAMES[@]}")
# shellcheck source=/dev/null
source "$SCRIPT_DIR/bin/core/start_funcs.sh"

trap 'log "serve-watcher 退出"; rm -rf "$SERVE_RESTART_LOCK" 2>/dev/null; exit 0' SIGTERM SIGINT

# probe_serve — 进程活 + HTTP /session 200 才算健康（与 healthcheck check_serve_http 同判据）
probe_serve() {
    local pid port pwd auth code
    pid=$(cat "$SCRIPT_DIR/.serve.pid" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    port=$(cat "$SCRIPT_DIR/.serve.port" 2>/dev/null)
    pwd=$(cat "$SCRIPT_DIR/.serve.pwd" 2>/dev/null)
    [ -n "$port" ] && [ -n "$pwd" ] || return 1
    auth=$(printf 'opencode:%s' "$pwd" | base64 | tr -d '\n')
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        -H "Authorization: Basic $auth" "http://127.0.0.1:$port/session" 2>/dev/null)
    [ "$code" = "200" ]
}

# 原子锁（带存活检测，避免持有者被 SIGKILL 后 stale 锁永久阻塞后续重拉）
acquire_restart_lock() {
    if mkdir "$SERVE_RESTART_LOCK" 2>/dev/null; then echo $$ > "$SERVE_RESTART_LOCK/pid"; return 0; fi
    local holder; holder=$(cat "$SERVE_RESTART_LOCK/pid" 2>/dev/null)
    if [ -z "$holder" ] || ! kill -0 "$holder" 2>/dev/null; then
        rm -rf "$SERVE_RESTART_LOCK" 2>/dev/null
        if mkdir "$SERVE_RESTART_LOCK" 2>/dev/null; then echo $$ > "$SERVE_RESTART_LOCK/pid"; return 0; fi
    fi
    return 1
}
release_restart_lock() { rm -rf "$SERVE_RESTART_LOCK" 2>/dev/null; }

restart_serve() {
    if ! acquire_restart_lock; then
        log "serve 重拉跳过：已有重拉进行中（锁 $SERVE_RESTART_LOCK）"
        return 0
    fi
    # 锁内复检：可能刚被 monitor 或上轮恢复
    if probe_serve; then release_restart_lock; return 0; fi
    # 杀残留 serve（pidfile 进程组 + 端口特征兜底），再统一重拉
    local old; old=$(cat "$SCRIPT_DIR/.serve.pid" 2>/dev/null)
    [ -n "$old" ] && { kill -9 -"$old" 2>/dev/null || kill -9 "$old" 2>/dev/null; }
    pkill -9 -f "opencode serve --port $SERVE_PORT" 2>/dev/null || true
    sleep 1
    start_serve
    sleep 2
    if probe_serve; then
        log "⚡ serve-watch：serve 异常，已单独重拉 serve pid=$(cat "$SCRIPT_DIR/.serve.pid" 2>/dev/null)（未等 monitor）"
    else
        log "⚠️ serve-watch：重拉后 serve 仍未就绪，交给 monitor 全量兜底"
    fi
    release_restart_lock
}

log "serve-watcher 启动（探测 ${SERVE_WATCH_INTERVAL}s/次，连续失败 ${SERVE_WATCH_FAILS} 次重拉，冷却 ${SERVE_WATCH_COOLDOWN}s）"
fails=0
while true; do
    sleep "$SERVE_WATCH_INTERVAL"
    if probe_serve; then
        [ "$fails" -gt 0 ] && log "serve-watch：serve 已恢复"
        fails=0
    else
        fails=$((fails + 1))
        log "serve-watch：serve 探测失败（$fails/${SERVE_WATCH_FAILS}）"
        if [ "$fails" -ge "$SERVE_WATCH_FAILS" ]; then
            restart_serve
            fails=0
            sleep "$SERVE_WATCH_COOLDOWN"
        fi
    fi
done
