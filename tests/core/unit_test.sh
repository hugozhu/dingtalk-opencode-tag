#!/bin/bash
# unit_test.sh — shell 单元测试模板
#
# 提炼自: dingtalk-opencode-agent/tests/unit_test.sh (v4.1, 50 tests)
# 原作者: hugozhu
#
# 测试对象:
#   - lib.sh 的 verify_pid / acquire_lock / release_lock / log
#   - monitor.sh 的 is_running / cleanup_stale_state / cleanup 退出码
#   - reboot.sh 的常量 + 失败传播
#
# 不依赖网络/钉钉/agent serve，纯 shell 函数级断言

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PASS=0
FAIL=0
FAILED_TESTS=()

assert_eq() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        echo -e "  \033[32m✓\033[0m $name"
        PASS=$((PASS + 1))
    else
        echo -e "  \033[31m✗\033[0m $name"
        echo "    expected: $expected"
        echo "    actual:   $actual"
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
    fi
}

# 测试 lib.sh
echo "Testing lib.sh..."

# 加载被测代码
source "$SCRIPT_DIR/bin/core/lib.sh"

# verify_pid 文件不存在时返回非 0
assert_eq "verify_pid 文件不存在返回非0" "1" "$(verify_pid /tmp/nonexistent.pid 'some-pattern' >/dev/null 2>&1; echo $?)"

# acquire_lock 第一次成功
LOCK=/tmp/test_harness_lock_$$
rm -f "$LOCK"
assert_eq "acquire_lock 第一次成功" "0" "$(acquire_lock "$LOCK"; echo $?)"
rm -f "$LOCK"

# release_lock 后能再 acquire
acquire_lock "$LOCK"
release_lock "$LOCK"
assert_eq "release_lock 后能再 acquire" "0" "$(acquire_lock "$LOCK"; echo $?)"
rm -f "$LOCK"

# log 输出格式
LOG_OUT=$(COMPONENT_NAME=test log "hello" 2>&1)
# 含 [YYYY-MM-DD HH:MM:SS] [test] hello
if [[ "$LOG_OUT" =~ \[20[0-9-]+\ [0-9:]+\]\ \[test\]\ hello ]]; then
    assert_eq "log 含时间戳 + 组件名" "1" "1"
else
    assert_eq "log 含时间戳 + 组件名" "1" "0 (actual: $LOG_OUT)"
fi

# 测试 monitor.sh 的常量默认值
echo ""
echo "Testing monitor.sh constants..."

# 用 bash -n 语法检查（不需要执行）
assert_eq "monitor.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/monitor.sh" 2>&1; echo $?)"
assert_eq "healthcheck.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/healthcheck.sh" 2>&1; echo $?)"
assert_eq "reboot.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/reboot.sh" 2>&1; echo $?)"
assert_eq "start.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/start.sh" 2>&1; echo $?)"
assert_eq "stop.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/stop.sh" 2>&1; echo $?)"
assert_eq "lib.sh 语法正确" "0" "$(bash -n "$SCRIPT_DIR/bin/core/lib.sh" 2>&1; echo $?)"

# 测试 lib.sh 的服务控制常量默认值（v4.2 重构后从 reboot.sh 移至 lib.sh）
KICKSTART_LINE=$(grep 'KICKSTART_RETRY_INTERVAL' "$SCRIPT_DIR/bin/core/lib.sh" | grep '=' | head -1)
if [[ "$KICKSTART_LINE" =~ KICKSTART_RETRY_INTERVAL:=[[:space:]]*\"?([0-9]+) ]]; then
    KICKSTART_VAL="${BASH_REMATCH[1]}"
else
    KICKSTART_VAL=""
fi
assert_eq "lib.sh KICKSTART_RETRY_INTERVAL=10" "10" "$KICKSTART_VAL"

LAUNCHD_LINE=$(grep 'LAUNCHD_LABEL' "$SCRIPT_DIR/bin/core/lib.sh" | grep '=' | head -1)
if [[ "$LAUNCHD_LINE" =~ LAUNCHD_LABEL:=[[:space:]]*\"?([a-zA-Z.]+) ]]; then
    LAUNCHD_VAL="${BASH_REMATCH[1]}"
else
    LAUNCHD_VAL=""
fi
assert_eq "lib.sh LAUNCHD_LABEL 存在" "1" "$([ -n "$LAUNCHD_VAL" ] && echo 1 || echo 0)"

# 测试 reboot.sh 的委托契约（v4.2：reboot 应调用 stop.sh 和 start.sh）
if grep -q "bin/core/stop.sh" "$SCRIPT_DIR/bin/core/reboot.sh" && \
   grep -q "bin/core/start.sh" "$SCRIPT_DIR/bin/core/reboot.sh"; then
    assert_eq "reboot.sh 委托 stop.sh + start.sh" "1" "1"
else
    assert_eq "reboot.sh 委托 stop.sh + start.sh" "1" "0 (reboot.sh 未引用 stop/start)"
fi

# 测试 README 不硬编码版本号（应指向 VERSION，避免漂移）
echo ""
echo "Testing version consistency..."
# README 里不应出现形如 `1.2.3` 的裸版本号（VERSION 是唯一真相源）
if grep -Eq '版本[:：].*`[0-9]+\.[0-9]+\.[0-9]+`' "$SCRIPT_DIR/README.md"; then
    assert_eq "README 不硬编码版本号" "1" "0 (README 出现硬编码版本，应指向 VERSION)"
else
    assert_eq "README 不硬编码版本号" "1" "1"
fi

# 测试 dws-connect.sh 的订阅选择逻辑（含新增 @我(at) 订阅）
echo ""
echo "Testing dws-connect.sh subscription selection..."
DWS_CONNECT="$SCRIPT_DIR/bin/custom/dws-connect.sh"

assert_eq "dws-connect.sh 语法正确" "0" "$(bash -n "$DWS_CONNECT" 2>&1; echo $?)"

# dry-run 纯 env 驱动（跳过 constants.local.sh），只打印订阅计划
_dwsplan() {
    env DWS_CONNECT_SKIP_LOCAL=1 DWS_CONNECT_DRY_RUN=1 CONNECT_LOG=/dev/null \
        "$@" bash "$DWS_CONNECT" 2>/dev/null
}

# 只开 @我：group/o2o 关，at 开，且起了 at consumer
AT_ONLY="$(_dwsplan DWS_PROFILE=p DWS_EVENT_AT=1)"
assert_eq "仅 AT: plan at=1" "1" "$(echo "$AT_ONLY" | grep -c 'plan: group=0 o2o=0 at=1')"
assert_eq "仅 AT: 起 at consumer" "1" "$(echo "$AT_ONLY" | grep -c 'consumer: user_im_message_receive_at')"

# 三种同时开
ALL="$(_dwsplan DWS_PROFILE=p DWS_EVENT_GROUP=cidX== DWS_EVENT_O2O_USERS=u1 DWS_EVENT_AT=true)"
assert_eq "全开: plan" "1" "$(echo "$ALL" | grep -c 'plan: group=1 o2o=1 at=1')"
assert_eq "全开: 含 at consumer" "1" "$(echo "$ALL" | grep -c 'consumer: user_im_message_receive_at')"

# AT 关（值为 0）不起 at consumer
OFF="$(_dwsplan DWS_PROFILE=p DWS_EVENT_GROUP=cidY== DWS_EVENT_AT=0)"
assert_eq "AT=0 不起 at consumer" "0" "$(echo "$OFF" | grep -c 'consumer: user_im_message_receive_at')"

# 什么都不配 → 报错退出非 0（at 也没开）
NONE_RC="$(env DWS_CONNECT_SKIP_LOCAL=1 DWS_CONNECT_DRY_RUN=1 CONNECT_LOG=/dev/null \
    DWS_PROFILE=p bash "$DWS_CONNECT" >/dev/null 2>&1; echo $?)"
assert_eq "无任何订阅 → 退出非0" "1" "$NONE_RC"

# 测试 #71 进程生命周期修复（_bus 孤儿清扫 + reboot 干净环境）
echo ""
echo "Testing #71 process lifecycle fixes..."

# dws-connect.sh：consumer 收尾必须走子树清理（否则 dws event _bus 甩成孤儿）
assert_eq "dws-connect.sh 定义 _kill_subtree" "1" \
    "$(grep -c '^_kill_subtree()' "$SCRIPT_DIR/bin/custom/dws-connect.sh")"
assert_eq "dws-connect.sh 有 EXIT/TERM 收尾 trap" "1" \
    "$(grep -q "trap '_cleanup_consumers' EXIT" "$SCRIPT_DIR/bin/custom/dws-connect.sh" && echo 1 || echo 0)"

# stop.sh / monitor.sh：调用 custom 停机钩子 stop_extra_cleanup
assert_eq "stop.sh 调用 stop_extra_cleanup 钩子" "1" \
    "$(grep -q 'stop_extra_cleanup' "$SCRIPT_DIR/bin/core/stop.sh" && echo 1 || echo 0)"
assert_eq "monitor.sh stop_all 调用 stop_extra_cleanup 钩子" "1" \
    "$(grep -q 'stop_extra_cleanup' "$SCRIPT_DIR/bin/core/monitor.sh" && echo 1 || echo 0)"

# reboot.sh：用干净环境跑 stop/start（否则改 config 后 /reboot 不生效）
assert_eq "reboot.sh 用 env -i 干净环境重启" "1" \
    "$(grep -q 'env -i' "$SCRIPT_DIR/bin/core/reboot.sh" && echo 1 || echo 0)"

# custom start_funcs.sh 语法 + 钩子定义
assert_eq "custom start_funcs.sh 语法正确" "0" \
    "$(bash -n "$SCRIPT_DIR/bin/custom/start_funcs.sh" 2>&1; echo $?)"

# 功能测试：stop_extra_cleanup 按 profile 精确清扫假 dws event 进程树，
# 不误伤其他 profile 的进程
FAKE_DIR=$(mktemp -d)
cat > "$FAKE_DIR/dws" <<'EOF'
#!/bin/bash
sleep 300 &
sleep 300
EOF
chmod +x "$FAKE_DIR/dws"
"$FAKE_DIR/dws" event consume --profile "unittest:fakebot" >/dev/null 2>&1 &
FAKE_PID=$!
disown "$FAKE_PID" 2>/dev/null
"$FAKE_DIR/dws" event consume --profile "unittest:otherbot" >/dev/null 2>&1 &
OTHER_PID=$!
disown "$OTHER_PID" 2>/dev/null
sleep 1

# 载入钩子（COMP_NAMES 置空避免覆盖逻辑报错；log 输出屏蔽）
COMP_NAMES=()
source "$SCRIPT_DIR/bin/custom/start_funcs.sh" 2>/dev/null
DWS_PROFILE="unittest:fakebot" stop_extra_cleanup KILL 2>/dev/null
sleep 1

assert_eq "stop_extra_cleanup 清扫匹配 profile 的 dws event" "1" \
    "$(kill -0 "$FAKE_PID" 2>/dev/null && echo 0 || echo 1)"
assert_eq "stop_extra_cleanup 不误伤其他 profile" "1" \
    "$(kill -0 "$OTHER_PID" 2>/dev/null && echo 1 || echo 0)"

# teardown：清掉另一棵假进程树 + 临时目录
kill_tree "$OTHER_PID" KILL 2>/dev/null
rm -rf "$FAKE_DIR"

# ---------------------------------------------------------------------------
# 监督器阻塞性 bug 的回归钉子
#
# 两个 bug 都属于「失效模式是静默」那一类，靠人看日志发现不了，所以钉死在测试里：
#   1. check_serve_http 失败时曾返回 "HTTP_FAIL:$port"，而 main() 的判定是 glob `FAIL*`
#      —— 匹配不上，serve HTTP 异常从来无法触发熔断
#   2. 那个 curl 没有超时，serve 卡死时 healthcheck 永不返回，monitor 监督循环停摆
# ---------------------------------------------------------------------------
echo ""
echo "Testing 健康检查判定 + 超时（监督器阻塞性 bug 回归）..."

assert_eq "check_serve_http 失败返回值能匹配 FAIL* 判定" "0" \
    "$(grep -q 'echo "FAIL: serve HTTP' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "healthcheck.sh 不再 echo 匹配不上判定的 HTTP_FAIL token" "0" \
    "$(grep -c 'echo "HTTP_FAIL' "$SCRIPT_DIR/bin/core/healthcheck.sh" || true)"
assert_eq "check_serve_http 的 curl 带硬超时" "0" \
    "$(grep -q -- '--max-time' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "monitor 用 run_with_timeout 包裹 healthcheck" "0" \
    "$(grep -q 'run_with_timeout .*healthcheck.sh' "$SCRIPT_DIR/bin/core/monitor.sh" && echo 0 || echo 1)"

# run_with_timeout 的实际行为（不是 grep，是真跑）
assert_eq "run_with_timeout 超时返回 124" "124" \
    "$(run_with_timeout 2 sleep 10 >/dev/null 2>&1; echo $?)"
assert_eq "run_with_timeout 正常完成时透传退出码 0" "0" \
    "$(run_with_timeout 5 true >/dev/null 2>&1; echo $?)"
assert_eq "run_with_timeout 正常完成时透传非零退出码" "3" \
    "$(run_with_timeout 5 bash -c 'exit 3' >/dev/null 2>&1; echo $?)"
# 超时那次必须真的把进程杀掉，不能留孤儿继续跑
TMO_MARK="$(mktemp -t rwt_mark)"
rm -f "$TMO_MARK"
run_with_timeout 2 bash -c "sleep 6; echo leaked > '$TMO_MARK'" >/dev/null 2>&1 || true
sleep 6
assert_eq "run_with_timeout 超时后子进程被真正杀死（无孤儿）" "1" \
    "$([[ -f "$TMO_MARK" ]] && echo 0 || echo 1)"
rm -f "$TMO_MARK"

# ---------------------------------------------------------------------------
# 大脑真实自检：计数窗口语义 + 钩子存在性
#
# 窗口语义每一条都对应一种误报/漏报：倒算历史 → 一重启就误判；累计而非窗口 → 一旦
# 坏过一次就永远超阈值；body 行被计数 → 模型输出能伪造健康状态。
# ---------------------------------------------------------------------------
echo ""
echo "Testing 大脑自检计数窗口（count_new_matches）..."

_BP='^\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}\] transport=(http|cli) .* ok=False'
CNT_LOG=$(mktemp); CNT_OFF="$CNT_LOG.off"; rm -f "$CNT_OFF"
_cnt_fail() { printf '[2026-08-08 09:18:35] transport=http model=m elapsed=1s prompt_len=1 reply_len=0 ok=False err=x\n' >> "$CNT_LOG"; }
_cnt() { count_new_matches "$CNT_LOG" "$CNT_OFF" "$_BP" "${1:-}" | awk '{print $3}'; }

_cnt_fail; _cnt_fail; _cnt_fail
assert_eq "首次运行不倒算历史失败" "0" "$(_cnt consume)"
_cnt_fail; _cnt_fail
assert_eq "统计新增 2 条" "2" "$(_cnt consume)"
assert_eq "窗口非累计（无新增归零）" "0" "$(_cnt consume)"
_cnt_fail
assert_eq "peek 不消费窗口（第一次）" "1" "$(_cnt)"
assert_eq "peek 不消费窗口（第二次仍可见）" "1" "$(_cnt)"
_cnt consume >/dev/null
printf '[2026-08-08 09:20:00] <<< RESP status=200 body={"t":"transport=http ok=False"}\n' >> "$CNT_LOG"
assert_eq "RESP body 里的 ok=False 不被计数（锚定行首）" "0" "$(_cnt consume)"
_cnt_fail; : > "$CNT_LOG"; _cnt_fail
assert_eq "日志被截断后从头计数" "1" "$(_cnt consume)"
CNT_LOG2=$(mktemp)
printf '[2026-08-08 09:18:35] transport=cli model=m elapsed=1s prompt_len=1 reply_len=0 ok=False\n' >> "$CNT_LOG2"
mv "$CNT_LOG2" "$CNT_LOG"
assert_eq "日志轮转（inode 变）后从头计数" "1" "$(_cnt consume)"
assert_eq "日志不存在时返回 0 且不报错" "0" \
    "$(count_new_matches /nonexistent/nope.log "$CNT_OFF" "$_BP" | awk '{print $3}')"
rm -f "$CNT_LOG" "$CNT_OFF"

echo ""
echo "Testing 大脑自检跨窗口累计（check_brain）..."

# 提取 check_brain 单个函数做隔离测试——healthcheck.sh 顶层有 main "$@"，不能整文件 source
eval "$(sed -n '/^check_brain()/,/^}$/p' "$SCRIPT_DIR/bin/core/healthcheck.sh")"

CB_LOG=$(mktemp); CB_OFF="$CB_LOG.off"; CB_PEND="$CB_LOG.pend"
rm -f "$CB_OFF" "$CB_PEND"
_cb_fail() { printf '[2026-09-17 03:01:46] transport=http model=m elapsed=65s prompt_len=1 reply_len=0 ok=False err=x\n' >> "$CB_LOG"; }
CB_PROBE_RC=0
brain_probe() { return "$CB_PROBE_RC"; }
AGENT_OPENCODE_LOG="$CB_LOG"
BRAIN_FAIL_OFFSET_FILE="$CB_OFF"
BRAIN_PENDING_FILE="$CB_PEND"
HEALTHCHECK_BRAIN_CHECK_ENABLED=1
HEALTHCHECK_BRAIN_FAIL_THRESHOLD=2
HEALTHCHECK_BRAIN_PROBE_TIMEOUT=1
SERVE_PORT_FILE=""
SERVE_PWD_FILE=""

consume=1
assert_eq "首次运行建基线（0 失败 0 累计）" "OK: 未消失败 0(<2)" "$(check_brain)"
_cb_fail
assert_eq "单条失败（CLI 回退救回的瞬时抖动）未达阈值不探针" "OK: 未消失败 1(<2)" "$(check_brain)"
assert_eq "无新增时失败证据跨窗口保留（2026-09-17 事故根因）" "OK: 未消失败 1(<2)" "$(check_brain)"
_cb_fail
assert_eq "累计达阈值触发探针（探针通过）" "OK: 探针通过 (未消失败 2)" "$(check_brain)"
assert_eq "探针通过清零未消失败" "0" "$(cat "$CB_PEND")"
assert_eq "清零后无新增归零" "OK: 未消失败 0(<2)" "$(check_brain)"

_cb_fail; _cb_fail
CB_PROBE_RC=1
assert_eq "探针失败报 FAIL" "FAIL: 大脑自检失败 (未消失败 2, )" "$(check_brain)"
assert_eq "探针失败保留失败证据（不摁下熔断连续计数）" "2" "$(cat "$CB_PEND")"
CB_PROBE_RC=0
assert_eq "证据保留使下个周期继续探针" "OK: 探针通过 (未消失败 2)" "$(check_brain)"

_cb_fail
echo 1 > "$CB_PEND"    # 模拟上一周期攒下 1 条未消失败
consume=""
CB_PEEK_OUT=$(check_brain)
assert_eq "peek 也按累计值裁决（1 攒 + 1 新 = 2 ≥ 阈值）" "OK: 探针通过 (未消失败 2)" "$CB_PEEK_OUT"
assert_eq "peek 不消费累计状态（startup_report/e2e 门禁只看不改）" "1" "$(cat "$CB_PEND")"
consume=1
rm -f "$CB_LOG" "$CB_OFF" "$CB_PEND"

assert_eq "brain 检查已接进硬失败判定列表" "0" \
    "$(grep -q '"brain|\$r_brain"' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "healthcheck 支持 --consume" "0" \
    "$(grep -q -- '--consume) consume=' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "monitor 守护循环传 --consume" "0" \
    "$(grep -q 'healthcheck.sh" --consume' "$SCRIPT_DIR/bin/core/monitor.sh" && echo 0 || echo 1)"
assert_eq "offset 文件登记进可清理状态表" "0" \
    "$(grep -q '.opencode-log.offset' "$SCRIPT_DIR/bin/core/lib.sh" && echo 0 || echo 1)"
assert_eq "brain 失败累计文件登记进可清理状态表" "0" \
    "$(grep -q '.brain-fail.pending' "$SCRIPT_DIR/bin/core/lib.sh" && echo 0 || echo 1)"
assert_eq "check_brain 持久化累计状态（跨窗口累计修复）" "0" \
    "$(grep -q 'BRAIN_PENDING_FILE' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "brain_probe.py 语法正确" "0" \
    "$(python3 -m py_compile "$SCRIPT_DIR/bin/custom/brain_probe.py" 2>&1; echo $?)"

# custom 钩子存在性（COMP_NAMES=() 见上文：未绑定数组在 set -u + bash 3.2 下是致命的）
COMP_NAMES=()
source "$SCRIPT_DIR/bin/custom/start_funcs.sh" >/dev/null 2>&1
assert_eq "custom 定义 brain_probe 钩子" "0" \
    "$(declare -F brain_probe >/dev/null 2>&1 && echo 0 || echo 1)"
assert_eq "custom 定义 notify_alert_handler 钩子（熔断告警不再静默）" "0" \
    "$(declare -F notify_alert_handler >/dev/null 2>&1 && echo 0 || echo 1)"

echo ""
echo "Testing 事件流新鲜度检查（check_event_freshness）..."

# 提取单个函数做隔离测试（同 check_brain 模式——healthcheck.sh 顶层有 main "$@"）
eval "$(sed -n '/^check_event_freshness()/,/^}$/p' "$SCRIPT_DIR/bin/core/healthcheck.sh")"

EF_LOG=$(mktemp)
MONITOR_LOG="$EF_LOG"
# 造「N 秒前」的入站时间戳（macOS date -r / Linux date -d @ 双写法）
_ef_ts_ago() {
    local e=$(( $(date +%s) - $1 ))
    date -r "$e" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date -d "@$e" '+%Y-%m-%d %H:%M:%S'
}
_ef_mk_inbound() {
    echo "[$(_ef_ts_ago "$1")] [agent] inbound: msgId=msg-$2 kind=text user=PiBot" >> "$EF_LOG"
}

EVENT_FRESHNESS_THRESHOLD=0
assert_eq "阈值为 0 → SKIP（禁用开关）" "SKIP: 未启用" "$(check_event_freshness)"

EVENT_FRESHNESS_THRESHOLD=7200
: > "$EF_LOG"
assert_eq "无入站记录 → SKIP（新部署不误报）" "SKIP: 尚无入站消息记录" "$(check_event_freshness)"

_ef_mk_inbound 100
assert_eq "100s 前有入站 → OK" "1" \
    "$(check_event_freshness | grep -q '^OK: 100s 前有入站$' && echo 1 || echo 0)"

: > "$EF_LOG"
_ef_mk_inbound 8000
assert_eq "8000s 无入站 → FAIL（2026-09-18 事故场景）" "1" \
    "$(check_event_freshness | grep -qE '^FAIL: 事件流 8000s 无入站消息' && echo 1 || echo 0)"

rm -f "$EF_LOG"

assert_eq "freshness 检查已接进硬失败判定列表" "0" \
    "$(grep -q '"freshness|\$r_freshness"' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "freshness 检查进 JSON checks 输出" "0" \
    "$(grep -q '"event_freshness"' "$SCRIPT_DIR/bin/core/healthcheck.sh" && echo 0 || echo 1)"
assert_eq "monitor 的 run_healthcheck 保留 WARN/FAIL 输出（修 WARN 三重静默）" "0" \
    "$(grep -q "grep -E 'WARN|FAIL'" "$SCRIPT_DIR/bin/core/monitor.sh" && echo 0 || echo 1)"

# 报告
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Results: $PASS passed, $FAIL failed, 0 skipped"
if [[ $FAIL -gt 0 ]]; then
    echo "Failed tests:"
    for t in "${FAILED_TESTS[@]}"; do
        echo "  - $t"
    done
    exit 1
fi
