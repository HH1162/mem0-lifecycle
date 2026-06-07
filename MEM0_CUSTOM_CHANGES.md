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

#### 解决方案

```python
def update_anchor_time():
    # 1. 读取旧 anchor
    old_anchor = get_anchor_from_state_file()
    
    # 2. 计算 gap (小时)
    gap_hours = (now - old_anchor).total_seconds() / 3600
    
    # 3. 如果 gap > 36h (离线超过1.5天)
    if gap_hours > 36:
        gap_days = gap_seconds / 86400
        
        # 遍历 Qdrant，把所有 last_accessed_at 向前平移 gap_days
        for pt in qdrant.scroll(...):
            new_la = old_la + timedelta(days=gap_days)
            qdrant.set_payload(...)
    
    # 4. 更新 anchor 为 now()
    write_state_file(now)
```

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

1. **Qdrant 不可用**: 捕获异常，不阻塞 search/add
2. **last_accessed_at='never'**: 跳过平移
3. **STATE_FILE 不存在**: 直接写入 now，不报错
4. **幂等性**: 多次调用不会重复加 gap（每次只基于当前 anchor 计算）

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
