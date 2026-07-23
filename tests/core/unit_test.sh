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
