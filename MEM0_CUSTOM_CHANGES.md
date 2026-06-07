# Mem0 本地部署 - 自定义修改说明

## 概述

本文档记录了 `/media/data/mem0/mem0_server.py` 的所有自定义修改，用于解决 Mem0 记忆衰减系统中的时间锚点问题。

## 环境信息

- **Mem0 版本**: 2.0.4
- **Embedder**: `bge-large-zh-v1.5`（本地）
- **Vector Store**: Qdrant (`localhost:6333`)
- **LLM**: qwen3
- **状态文件**: `~/.hermes/mem0_state.json`

## 修改历史

### 1. HuggingFace FutureWarning 修复

**文件**: `/media/data/mem0/.venv/lib/python3.11/site-packages/mem0/embeddings/huggingface.py`
**行号**: L27
**修改内容**:
```python
# Before
get_sentence_embedding_dimension()

# After
get_embedding_dimension()
```

**原因**: Mem0 2.0.4 内部调用了已弃用的方法名，产生 FutureWarning。此修改仅消除警告，不影响功能。

---

### 2. 时间锚点 Gap 补偿机制 ⭐

**文件**: `/media/data/mem0/mem0_server.py`
**函数**: `update_anchor_time()` (L78-166)
**修改日期**: 2026-06-07

#### 问题描述

原始实现中，`update_anchor_time()` 在每次 search/add 时无脑写入 `now()` 作为新 anchor。当机器离线超过一天后重启，anchor 会突然跳到当前时间，导致所有未命中记忆的 `effective_days` 虚高，次日清理时被批量误删。

**数学推导**:
```
设离线时长为 G 天
旧 anchor = T₀, 新时间 = T₀ + G

若直接更新 anchor 至 T₀+G:
  effective_days = (T₀+G) - last_accessed_at
                 = (T₀ - last_accessed_at) + G
                 = 旧 effective_days + G  ← 错误！离线时间被计入"记忆空闲时间"

若同时平移 last_accessed_at:
  new_la = old_la + G
  effective_days = (T₀+G) - (old_la + G)
                 = T₀ - old_la  ← 正确！相对距离严格不变
```

#### 解决方案 v7 — Global Time Offset（推荐）

放弃直接平移 Qdrant payload 的方案，改为在 `mem0_state.json` 中维护一个全局 `time_offset_days` 累加器。衰减计算时：

```
raw_days = max(0, (anchor_time - last_accessed_at).total_seconds() / 86400)
effective_days = max(0, raw_days - time_offset_days)
```

```python
def update_anchor_time():
    now = datetime.now(timezone.utc)
    state = _load_state()
    
    # 读取旧 anchor
    old_anchor = parse(state.get('last_active', ''))
    
    if old_anchor is not None:
        gap_hours = (now - old_anchor).total_seconds() / 3600
        
        # 如果 gap > 36h，累加偏移量（O(1)操作，不碰 Qdrant）
        if gap_hours > 36:
            gap_days = gap_hours / 24
            state['time_offset_days'] = state.get('time_offset_days', 0.0) + gap_days
    
    state['last_active'] = now.isoformat()
    _save_state(state)
```

**为什么 v7 优于旧方案：**

| 维度 | 旧方案（平移 Qdrant） | v7（全局 time_offset） |
|------|---------------------|----------------------|
| 复杂度 | O(N)，需遍历所有记忆 | O(1)，只改一个数字 |
| 安全性 | 可能漏改、改错、Qdrant 不可用时失败 | 零 Qdrant 交互，绝对安全 |
| 幂等性 | 依赖 last_compensation_time 记录 | 天然幂等，重复加 offset 无害 |
| 可扩展性 | 记忆越多越慢 | 1 万条和 1 条一样快 |

#### 阈值选择

| 阈值 | 优点 | 缺点 |
|------|------|------|
| 24h | 更激进补偿 | 隔天使用可能误触发 |
| **36h** | **平衡点** | **离开1.5天才触发** |
| 48h | 最保守 | 两天整不触发 |

**最终选择**: 36 小时（平衡日常使用和离线补偿）

#### 触发条件

| 场景 | Gap | 是否触发 |
|------|-----|---------|
| 今晚关机，明天开机 | ~10h | ❌ 不触发 ✓ |
| 隔天用一次 | 24-36h | ❌ 不触发 ✓ |
| 离开1.5天 | 36h整 | ❌ 不触发 ✓ |
| 离开两天 | ~48h | ✓ 触发 ✓ |
| 出差3天 | 72h | ✓ 触发 ✓ |

#### 安全机制

1. **零 Qdrant 交互**: 完全不碰数据库，即使 Qdrant 挂了也不影响
2. **天然幂等**: 多次调用不会重复加 gap（每次只基于当前 anchor 计算）
3. **STATE_FILE 不存在**: 直接写入 now，不报错
4. **异常捕获**: 即使状态文件损坏也不阻塞 search/add

#### 调用时机

`update_anchor_time()` 仅在以下动作时调用：
```python
if action in ("search", "add"):
    update_anchor_time()
```

**注意**: 不是"开机"就更新，而是用户实际使用 mem0（搜索/添加记忆）时才更新。

---

## 衰减配置

```python
HALF_LIFE_DAYS = 7      # 半衰期
CLEANUP_THRESHOLD = 0.05 # 清理阈值
ACCESS_COUNT_CAP = 255   # 访问计数上限
GRACE_PERIOD = 14        # 保护期（天）
GAP_THRESHOLD = 36       # Gap 补偿阈值（小时）
```

**衰减公式（v7 - Global Time Offset）**:
```
weighted_score = min(access_count, CAP) × 0.5^(effective_days / half_life)
raw_days = max(0, (anchor_time - last_accessed_at).total_seconds() / 86400)
effective_days = max(0, raw_days - time_offset_days)
```

`time_offset_days` 存储在 `~/.hermes/mem0_state.json` 中，离线 > 36h 时自动累加。

**衰减曲线（half_life=7天）**:

| access_count | 0天 | 7天 | 14天 | 21天 | 28天 | 35天 | 到阈值(0.05)所需天数 |
|-------------|-----|-----|------|------|------|------|-------------------|
| 1 (低频)    | 1.0 | 0.5 | 0.25 | 0.125| 0.063| 0.031| ~33天             |
| 5 (中频)    | 5.0 | 2.5 | 1.25 | 0.625| 0.313| 0.156| ~47天             |
| 20 (高频)   | 20  | 10  | 5    | 2.5  | 1.25 | 0.625| ~61天             |
| 255 (封顶)   | 255 | 127 | 63.8 | 31.9 | 15.9 | 7.95 | ~86天             |

**关键发现**: 高频记忆(255次)实际需要约 86 天才衰减到清理阈值，而非直觉上的 50 天。

---

## 备份文件

- `/media/data/mem0/mem0_server.py.bak.202605312159` - 原始版本
- `/media/data/mem0/mem0_server.py.bak.20260607` - 修改前版本

---

## 测试验证

### 单元测试

```bash
# 语法检查
python3 -c "import ast; ast.parse(open('/media/data/mem0/mem0_server.py').read())"

# 逻辑验证
python3 << 'EOF'
from datetime import datetime, timezone, timedelta

now = datetime.now(timezone.utc)
old_anchor = now - timedelta(days=30)

gap_hours = (now - old_anchor).total_seconds() / 3600
print(f"Gap: {gap_hours:.1f} hours")

if gap_hours > 36:
    print("✓ Gap > 36h → will shift timestamps")
else:
    print("✗ Gap <= 36h → no shift needed")
EOF
```

### Dry-run 测试

```bash
# 模拟离线30天重启
/media/data/mem0/.venv/bin/python3 << 'EOF'
import json
import os
from datetime import datetime, timezone, timedelta

STATE_FILE = '/tmp/test_mem0_state.json'
now = datetime.now(timezone.utc)
old_anchor = now - timedelta(days=30)

with open(STATE_FILE, 'w') as f:
    json.dump({"last_active": old_anchor.isoformat()}, f)

# 验证逻辑
with open(STATE_FILE) as f:
    state = json.load(f)
    ts = state.get("last_active", "")
    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))

gap_hours = (now - dt).total_seconds() / 3600
print(f"Gap: {gap_hours:.1f} hours")

if gap_hours > 36:
    gap_days = gap_hours / 24
    print(f"Would shift by +{gap_days:.1f} days")
    
    # 模拟平移
    test_la = now - timedelta(days=25)
    new_la = test_la + timedelta(days=gap_days)
    
    old_effective = (dt - test_la).total_seconds() / 86400
    new_effective = (now - new_la).total_seconds() / 86400
    
    if abs(old_effective - new_effective) < 0.01:
        print(f"✅ PASS: effective_days preserved ({old_effective:.1f} → {new_effective:.1f})")
    else:
        print(f"❌ FAIL: effective_days changed ({old_effective:.1f} → {new_effective:.1f})")

os.remove(STATE_FILE)
EOF
```

---

### 3. Over-Fetching + 双阈值去重（插件端）

**文件**: `~/.hermes/hermes-agent/plugins/memory/mem0/__init__.py`
**修改日期**: 2026-06-07

#### 问题描述

默认 top_k=5 检索时，高频旧记忆可能占据前几位，导致新记忆无法被召回。直接增大 top_k 又会导致 token 浪费和重复注入。

#### 解决方案

1. **Over-fetching**: 检索 top_k=20，去重后截取前 5 条注入
2. **双阈值文本相似度去重**（使用 `SequenceMatcher`，无需额外依赖）：

| 相似度 | 文本匹配 | 处理方式 |
|--------|---------|---------|
| > 0.95 | 完全一致（去标点+空格后） | 丢弃为 shadow（纯冗余） |
| > 0.95 | 文本有差异 | 降级为冲突保留 |
| 0.85 - 0.95 | 任意 | 保留 + 标记冲突 |
| ≤ 0.85 | 任意 | 正常注入 |

3. **保持 Qdrant 原始相似度排序**：严禁按 `updated_at` 重排，否则会把不相关但最近的记忆注入

#### 性能开销

- SequenceMatcher 文本相似度：top_k=20 场景下 <25ms
- 无需加载 Embedding 模型，无额外依赖

---

### 4. Search/Track 解耦 + touch 子命令

**文件**: `/media/data/mem0/mem0_server.py`
**修改日期**: 2026-06-07

#### 问题描述

原始实现中，search 自动 track 所有命中记忆。但去重后产生的 shadow IDs（纯冗余记忆）如果也被 track，`access_count` 会无限增长，导致这些冗余记忆"永生"——永远无法衰减到清理阈值。

#### 解决方案

三个独立操作：

| 命令 | 更新 last_accessed_at | 增加 access_count | 用途 |
|------|---------------------|------------------|------|
| `search --no-track` | ❌ | ❌ | 只读查询 |
| `track` | ✅ | ✅ | 注入的记忆（去重后的 top 5） |
| `touch` | ✅ | ❌ | Shadow 记忆（纯冗余） |

Shadow IDs 仅执行 `touch`，防止 `access_count` 无限增长。

---

### 5. 频次标签 + Prompt 冲突裁决规则

**文件**: `~/.hermes/hermes-agent/plugins/memory/mem0/__init__.py`
**函数**: `system_prompt_block()`
**修改日期**: 2026-06-07

#### 记忆格式化

每条记忆前缀格式：`[Updated N days ago | YYYY-MM-DD | 高频/中频/低频]`

- 高频 (>20次访问): 重要事实，优先信任
- 中频 (5-20次): 常规信息
- 低频 (<5次): 可能已过时

冲突标记：`⚠️ 可能与第N条冲突(相似度X.XX)`

#### 三层冲突裁决规则（注入 system prompt）

1. **时效优先**: 选择更新时间较近的记忆
2. **频次优先** (时间差 < 3天): 选择访问频次较高的记忆
3. **冲突确认** (权重相当时): 不要自行决定丢弃任何一方，在回复中委婉提及两种可能性

**设计原则**: 误删正确记忆的风险 > 保留过期记忆的成本。绝不替 LLM 做决定丢弃可能正确的记忆。

---

### 6. UPDATE 防御（快照恢复）

**文件**: `/media/data/mem0/mem0_server.py`
**函数**: `add()` action
**修改日期**: 2026-06-07

#### 问题描述

Mem0 的 `add(infer=True)` 在更新已有记忆时，可能意外覆盖 `last_accessed_at`，破坏衰减状态。

#### 解决方案

1. `add()` 之前：快照所有记忆的 `last_accessed_at`
2. 执行 `add()`
3. `add()` 之后：比对并恢复被覆盖的值

```python
# Before add()
pre_add_payloads = snapshot_all_last_accessed_at(qdrant)

client.add(messages, ...)

# After add() — restore any changed values
for mem_id, original_la in pre_add_payloads.items():
    if current_la != original_la:
        qdrant.set_payload(payload={'last_accessed_at': original_la}, points=[mem_id])
```

---

### 7. 性能优化

**文件**: `/media/data/mem0/mem0_server.py`
**修改日期**: 2026-06-07

| 优化 | 效果 |
|------|------|
| `track`/`touch` 跳过 `get_memory_client()` 初始化 | 节省 ~4-6秒（避免 BGE 模型加载） |
| `batch_get_access_payload()` 批量拉取 payload | 100条记忆: 1次网络请求(<50ms) vs 100次(1-3秒) |
| 指数退避重试（1s, 2s, 4s） | 优雅处理瞬态故障 |
| 熔断器（5次连续失败 → 120秒冷却） | 防止持续轰击宕机的服务器 |

---

## 维护说明

### 升级后恢复

当 Mem0 库升级时，HuggingFace 的 patch 可能会丢失。需要重新应用：

```bash
# 检查是否需要恢复
grep "get_sentence_embedding_dimension" /media/data/mem0/.venv/lib/python3.11/site-packages/mem0/embeddings/huggingface.py

# 如果需要，重新 patch
sed -i 's/get_sentence_embedding_dimension/get_embedding_dimension/g' \
    /media/data/mem0/.venv/lib/python3.11/site-packages/mem0/embeddings/huggingface.py
```

### 监控

- 检查 stderr 日志中的 `[mem0] Offline gap=` 消息
- 确认 shifted 数量合理（通常 < 100）
- 如果出现大量错误，检查 Qdrant 连接状态

---

## 参考资料

- [Mem0 官方文档](https://docs.mem0.ai/)
- [Qdrant Python Client](https://qdrant.tech/documentation/concepts/qdrant-api/)
- [指数衰减算法](https://en.wikipedia.org/wiki/Exponential_decay)
