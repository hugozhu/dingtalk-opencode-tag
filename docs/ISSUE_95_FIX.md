# Issue #95 Fix: 长任务进度表情原地更新

## 问题描述

长任务进度心跳功能（#75）每 `ACK_PROGRESS_INTERVAL`（默认 300s）更新消息上的文字表情，从「处理中5分钟」→「处理中10分钟」→「处理中15分钟」。设计约定**任一时刻消息上只有一个文字表情**，状态升级应走 `update-text-emotion` 原地更新。

但实际运行时，任务超过 15 分钟后，消息上出现**多个**进度文字表情同时挂着，没有做到 in-place 更新。

## 根本原因

问题出在 #82 引入的缓存 key 标准化逻辑：

1. **`_emotion_id` 函数把所有「处理中{N}分钟」归一到模板 key `处理中{mins}分钟`**：
   - 首次心跳（5 分钟）调用 `create-text-emotion` 创建 emotionId=A（文字为「处理中5分钟」），缓存到模板 key
   - 后续心跳（10/15 分钟）都命中缓存，返回同一个 emotionId=A

2. **`_update_text_emotion` 用缓存反查 old/new emotionId**：
   - 10 分钟心跳时，old=「处理中5分钟」→查缓存得 emotionId=A，new=「处理中10分钟」→查缓存得 emotionId=A
   - 调用 `update-text-emotion --old-emotion-id A --emotion-id A`（old==new）
   - 服务端不产生原地更新效果

3. **update 失败后的兜底 remove+add 也用同一个 emotionId**：
   - remove 用 emotionId=A（从缓存查「处理中5分钟」），但消息上实际已经是「处理中10分钟」，语义错位
   - add 也是 emotionId=A，旧表情没被移除，新表情照贴 → 多条进度表情共存

## 解决方案

### 1. 移除进度文字的缓存 key 标准化

修改 `_emotion_id` 函数，移除「处理中{N}分钟」→「处理中{mins}分钟」的模板归一逻辑。**每个不同的「处理中N分钟」创建独立 emotionId**：

```python
def _emotion_id(emoji, text):
    """按 (表情名, 文字) 拿到 (emotionId, backgroundId)，进程内缓存；首次 create。

    #95 fix：移除进度文字的缓存 key 标准化。每个不同的「处理中N分钟」创建独立 emotionId，
    使 update-text-emotion 能正确识别 old != new 并原地更新。静态文字（收到/完成/失败）
    仍缓存复用。
    """
    key = (emoji, text)  # 不再归一模板
    # ... 缓存查找 + create 逻辑不变
```

- 「处理中5分钟」→ emotionId=10
- 「处理中10分钟」→ emotionId=20
- 重复的「处理中5分钟」→ 缓存命中，复用 emotionId=10

### 2. `_Pending` 记录实际挂载的 emotionId

扩展 `_Pending` 类，新增 `cur_eid` 和 `cur_bid` 字段，记录当前实际挂在消息上的 emotionId/backgroundId：

```python
class _Pending:
    __slots__ = ("conv_id", "conv_type", "msg_id", "event", "ok", "cur", "cur_eid", "cur_bid")

    def __init__(self, conv_id, conv_type, msg_id):
        # ...
        self.cur = None           # 当前贴着的 (表情, 文字)
        self.cur_eid = None       # 当前贴着的 emotionId（#95：update 需精确定位旧表情）
        self.cur_bid = None       # 当前贴着的 backgroundId
```

### 3. 修改 `_update_text_emotion` 接收显式参数

不再从缓存反查 emotionId，而是接收调用方传入的实际挂载 ID：

```python
def _update_text_emotion(conv_id, msg_id, old_eid, old_bid, new_emoji, new_text, new_eid, new_bid):
    """原地更新文字表情：把 old emotionId 直接改成 new (表情,文字,emotionId)。

    #95 fix：接收实际挂载的 old_eid（由 _Pending.cur_eid 传入），不再从缓存反查。
    避免缓存漂移导致 old==new 而无法原地更新。
    """
    if not old_eid or not new_eid:
        return False
    args = ["chat", "message", "update-text-emotion",
            "--conversation-id", conv_id, "--msg-id", msg_id,
            "--old-emotion-id", old_eid,
            "--emotion-id", new_eid, "--emotion-name", new_emoji, "--text", new_text]
    if new_bid:
        args += ["--background-id", new_bid]
    # ...
```

### 4. 修改 `_set_status` 追踪 emotionId

在每次状态切换时，更新 `rec.cur_eid` 和 `rec.cur_bid`，并使用实际挂载的 ID 进行 update/remove 操作：

```python
def _set_status(rec, status):
    # ...
    # 准备新状态的 emotionId
    new_eid = new_bid = None
    if status:
        new_eid, new_bid = _emotion_id(status[0], status[1])
        if not new_eid:
            return

    if rec.cur and status:
        # 升级：原地更新（用实际挂载的 rec.cur_eid 作为 old）
        if not _update_text_emotion(rec.conv_id, rec.msg_id, rec.cur_eid, rec.cur_bid,
                                     status[0], status[1], new_eid, new_bid):
            # update 失败，兜底 remove（用 rec.cur_eid）+ add
            # ...

    # 更新状态：记录实际挂载的 emotionId/backgroundId
    rec.cur = status
    rec.cur_eid = new_eid if status else None
    rec.cur_bid = new_bid if status else None
```

## 修改文件

- `src/custom/capabilities/ack.py`
  - `_Pending` 类：新增 `cur_eid`、`cur_bid` 字段
  - `_emotion_id`：移除缓存 key 标准化（删除 14 行模板归一逻辑）
  - `_update_text_emotion`：签名改为接收显式 emotionId 参数
  - `_set_status`：追踪并使用实际挂载的 emotionId

- `tests/custom/test_ack_capability.py`
  - `TestEmotionCache`：新增 `test_progress_texts_create_unique_ids` 测试
  - `TestSetStatus`：更新测试以验证 old_eid != new_eid
  - `TestUpdateTextEmotion`：更新测试以匹配新签名
  - `TestProcessingAndFinalize`：更新 mock 以提供 emotionId
  - `TestLifecycleWorker`、`TestProgressHeartbeat`：更新 mock 工具函数

## 验收结果

✅ 所有 47 个单测通过（包括新增的 #95 专项测试）

关键验证点：
1. **每个不同的「处理中N分钟」创建独立 emotionId**（`test_progress_texts_create_unique_ids`）
2. **update 使用实际挂载的 emotionId，old != new**（`TestSetStatus.test_add_update_remove`）
3. **update 失败兜底时，remove 使用实际挂载的 emotionId**（`TestSetStatus.test_upgrade_falls_back_to_remove_add`）
4. **生命周期 worker 正常运行，进度心跳可正常更新表情**（`TestProgressHeartbeat.*`）

## 理论验证

修复后的行为：

1. **5 分钟心跳**：
   - 创建 emotionId=10（文字「处理中5分钟」），贴到消息上
   - `rec.cur_eid = 10`

2. **10 分钟心跳**：
   - 创建 emotionId=20（文字「处理中10分钟」）
   - 调用 `update-text-emotion --old-emotion-id 10 --emotion-id 20`（old != new ✓）
   - 服务端原地更新：「处理中5分钟」→「处理中10分钟」
   - `rec.cur_eid = 20`

3. **15 分钟心跳**：
   - 创建 emotionId=30（文字「处理中15分钟」）
   - 调用 `update-text-emotion --old-emotion-id 20 --emotion-id 30`（old != new ✓）
   - 服务端原地更新：「处理中10分钟」→「处理中15分钟」
   - `rec.cur_eid = 30`

**消息上始终只有一个进度文字表情，旧分钟数表情被原地替换** ✓

## 性能影响

- **缓存效率**：每个不同的「处理中N分钟」首次需要调用 `create-text-emotion`（约 15s timeout）
  - 对于 5min 间隔的心跳，最多每 5 分钟一次 create 调用（可接受）
  - 相同分钟数（如多条消息都在第 5 分钟）仍复用缓存（LRU 有效）
  
- **静态文字不受影响**：「收到」「完成」「失败」等静态文案仍全局缓存复用

## 生产部署建议

1. 无需配置变更，向后兼容
2. 重启服务后自动生效（缓存是进程内的，重启自然清空）
3. 建议测试：用一个长任务（>15 分钟）验证进度表情是否原地更新，无残留

## 相关 Issue

- #95: 长任务进度表情要 in-place 更新（本修复）
- #82: 缓存 key 标准化（引入问题的原始 PR）
- #85: 原地更新 API（update-text-emotion 基础设施）
- #75: 活动感知超时 + 周期进度心跳（问题显现场景）
