#!/usr/bin/env bash
# leak_check.sh — 测试有没有往**生产** knowledge/ 里写东西
#
# 这个仓库已经因为同一类问题漏过五次：审核流水 679 行、裁决反馈 266 条、图片描述、
# 31 条知识库夹具（把 system prompt 实际注入的 20 条挤掉了大半）、文件/语音转写。
# 每次都是"某个测试开了某个能力，但忘了把存储指到 tmpdir"。
#
# 靠人记不住，靠静态扫描误报太多（大部分测试 import 了能力却并不写盘）。这里用行为式
# 的办法：**把 PROJECT_DIR 指到临时目录**跑一遍全套。msgstore/知识库的默认路径都是相对
# PROJECT_DIR 解析的，所以任何忘了隔离的测试会写到临时目录里而不是生产 —— 跑完看那里
# 有没有东西就知道谁漏了，而且这一趟本身绝不会污染生产。
#
# 用法：bash tests/custom/leak_check.sh
set -uo pipefail
cd "$(dirname "$0")/../.."

SANDBOX=$(mktemp -d -t leakcheck-XXXXXX)
trap 'rm -rf "$SANDBOX"' EXIT

echo "沙箱 PROJECT_DIR: $SANDBOX"
echo

failed=0
for t in tests/custom/test_*.py tests/custom/e2e_*.py; do
    name=$(basename "$t")
    # 关键：清掉隔离变量再指走 PROJECT_DIR —— 让"忘记隔离"这件事暴露出来而不是被掩盖
    out=$(env -u AGENT_MSGSTORE_DIR -u AGENT_KNOWLEDGE_FILE -u SUPERVISOR_REVIEW_JOURNAL \
             PROJECT_DIR="$SANDBOX" timeout 180 python3 "$t" 2>&1)
    rc=$?
    leaked=$(find "$SANDBOX" -type f 2>/dev/null | head -20)
    if [[ -n "$leaked" ]]; then
        echo "❌ $name 往生产路径写了东西："
        sed 's|^'"$SANDBOX"'/|    |' <<<"$leaked"
        rm -rf "${SANDBOX:?}"/*
        failed=1
    elif [[ $rc -ne 0 ]]; then
        # 测试本身失败不是泄漏，但也值得报出来（沙箱里跑可能缺依赖，如真机 e2e）
        echo "⚠️  $name 退出码 $rc（非泄漏，可能是需要真实环境的用例）"
    fi
done

echo
if [[ $failed -eq 0 ]]; then
    echo "✅ 没有测试往生产 knowledge/ 写东西"
else
    echo "上面这些测试要在文件顶部把存储指到 tmpdir，例如："
    echo '    os.environ["AGENT_MSGSTORE_DIR"] = tempfile.mkdtemp(prefix="test-ms-")'
    echo '    os.environ["AGENT_KNOWLEDGE_FILE"] = os.path.join(_TMP, "qa.jsonl")'
fi
exit $failed
