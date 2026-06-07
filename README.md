# Mem0 Memory Lifecycle — Auto-Cleanup with Ebbinghaus Decay

> A production-tested memory lifecycle system that makes Mem0 behave like human memory — frequently accessed information persists, unused information naturally decays. Includes conflict resolution, deduplication, and offline gap compensation.

## The Problem

When running Mem0 locally, memories accumulate indefinitely. There is no expiration, no decay, no distinction between a preference retrieved daily and a temporary note created once and forgotten. Over time:

- **Retrieval noise compounds** — top-k results mix useful facts with stale artifacts, inflating context tokens and introducing contradictions
- **Stale memories pollute answers** — outdated preferences and expired task progress logs show up alongside current facts
- **Conflicting memories cause hallucination** — when old and new facts contradict, the LLM has no guidance on which to trust
- **Zero user intervention** — no built-in mechanism exists to address this without modifying Mem0 core

This project solves these problems by implementing:
1. An **exponential decay model inspired by the Ebbinghaus forgetting curve**
2. **Intelligent deduplication** that preserves recall while eliminating redundancy
3. **Conflict resolution rules** injected into the LLM prompt for safe adjudication
4. **Offline gap compensation** that prevents machine downtime from triggering mass deletions

No core Mem0 modification required — sits as a bridge layer between your application and Mem0.

## How It Works

### The Forgetting Curve Model

```python
weighted_score = min(access_count, 255) * 0.5 ** (effective_days / 7)
effective_days = max(0, raw_days - time_offset_days)
```

This formula mirrors how human memory consolidation works:

| Mechanism | Biology | Implementation |
|-----------|---------|----------------|
| **Repetition strengthens memory** | Repeated exposure reinforces neural pathways | `access_count` incremented on every search hit |
| **Time weakens memory** | Unused connections naturally fade | Exponential decay halves score every 7 days |
| **Diminishing returns on repetition** | After enough exposure, additional repetition adds little | `access_count` capped at 255 |
| **Recent events are protected** | New memories need consolidation time | 14-day grace period for newly created entries |
| **Below threshold = forgotten** | When a memory trace becomes too weak, it's lost | Score < 0.05 triggers deletion |

**Decay curve (half_life=7 days):**

| access_count | Day 0 | Day 7 | Day 14 | Day 21 | Day 28 | Day 35 | Days to threshold (0.05) |
|-------------|-------|-------|--------|--------|--------|--------|--------------------------|
| 1 (low freq) | 1.0 | 0.5 | 0.25 | 0.125 | 0.063 | 0.031 | ~33 days |
| 5 (mid freq) | 5.0 | 2.5 | 1.25 | 0.625 | 0.313 | 0.156 | ~47 days |
| 20 (high freq) | 20 | 10 | 5 | 2.5 | 1.25 | 0.625 | ~61 days |
| 255 (capped) | 255 | 127 | 63.8 | 31.9 | 15.9 | 7.95 | ~86 days |

**Key insight**: High-frequency memories (255 accesses) take ~86 days to decay to the cleanup threshold, not the intuitive 50 days.

### Architecture

```
Application ←→ mem0_lifecycle (bridge) ←→ Mem0 SDK ←→ Qdrant
                         ↓
   Over-fetching + dedup + conflict resolution + access tracking + decay scoring + automated cleanup
```

The bridge intercepts search calls, over-fetches from Qdrant (top_k=20), deduplicates using text similarity, tracks access frequency, computes decay scores, and automates cleanup — all transparent to the calling application.

## Installation

Install via pip:

```bash
pip install git+https://github.com/HH1162/mem0-agentic-enhancement-plugin.git
```

## Configuration

### Decay Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HALF_LIFE_DAYS` | 7 | Half-life period for decay |
| `CLEANUP_THRESHOLD` | 0.05 | Minimum weighted score to keep |
| `ACCESS_COUNT_CAP` | 255 | Maximum access count |
| `GRACE_PERIOD` | 14 days | Memories created within grace period are protected |
| `GAP_THRESHOLD` | 36 hours | Offline duration that triggers time offset accumulation |

### Time Anchor System

To prevent offline time from being counted as "memory idle time", this implementation uses a **time anchor** combined with a **global time offset**:

- **Anchor persistence**: Stored in `~/.hermes/mem0_state.json` (survives restarts)
- **Global time offset (v7)**: When offline > 36 hours, the gap is accumulated into `time_offset_days`. During decay calculation, `effective_days = max(0, raw_days - time_offset_days)`. This is an O(1) operation — no Qdrant writes needed, naturally idempotent, and scales infinitely.
- **Mathematical guarantee**: Preserves relative distance between anchor and `last_accessed_at`, preventing offline time from inflating `effective_days`

Example: Machine off for 30 days
```
raw_days = (now - last_accessed_at).days = 30
time_offset_days = 30
effective_days = max(0, 30 - 30) = 0 ✓
```

## Usage

### Basic Setup

```python
from mem0_lifecycle import Mem0LifecycleBridge

# Initialize the bridge
bridge = Mem0LifecycleBridge(
    mem0_client=your_mem0_client,
    half_life_days=7,
    cleanup_threshold=0.05,
    grace_period_days=14
)

# Search with access tracking
results = bridge.search("query text")

# Manual cleanup
bridge.cleanup()
```

### Automated Cleanup

Set up a cron job or scheduled task to run cleanup periodically:

```bash
# Daily cleanup at 2 AM
0 2 * * * python -c "from mem0_lifecycle import run_cleanup; run_cleanup()"
```

## Advanced Features

### Over-Fetching with Deduplication

Retrieves `top_k=20` from Qdrant, deduplicates, then injects top 5 into the prompt. This solves high-frequency old memories occupying limited slots and improves recall.

**Dual-threshold text similarity deduplication** (using `SequenceMatcher`, no extra dependencies):

| Similarity | Text Match | Action |
|------------|-----------|--------|
| > 0.95 | Exact match after cleaning | Discard as shadow (pure redundant) |
| > 0.95 | Text differs | Retain as conflict |
| 0.85 - 0.95 | Any | Retain + mark conflict |
| ≤ 0.85 | Any | Normal injection |

**Critical**: Qdrant's original similarity order is preserved. We never re-sort by `updated_at` — that would inject irrelevant memories just because they're recent.

### Search/Track Decoupling

Three distinct operations for fine-grained control:

| Command | Updates `last_accessed_at` | Increments `access_count` | Use case |
|---------|--------------------------|--------------------------|----------|
| `search --no-track` | ❌ | ❌ | Read-only queries |
| `track` | ✅ | ✅ | Injected memories (top 5 after dedup) |
| `touch` | ✅ | ❌ | Shadow memories (pure duplicates) |

**Why `touch` matters**: Shadow IDs (pure duplicates) only receive `touch`, NOT `track`. This prevents their `access_count` from growing indefinitely, which would make them "immortal" — they can never decay below the threshold if their count keeps increasing.

### Frequency Labels

Each memory is formatted with a frequency label visible to the LLM:

```
[Updated N days ago | YYYY-MM-DD | 高频/中频/低频] Memory text
```

- **高频 (>20 accesses)**: Important fact, prioritize trust
- **中频 (5-20 accesses)**: Regular information
- **低频 (<5 accesses)**: May be outdated

### Conflict Resolution

When conflicting memories are detected, three-layer rules are injected into the LLM's system prompt:

1. **时效优先 (Recency first)**: Choose the more recently updated memory (smaller "Updated N days ago")
2. **频次优先 (Frequency first, when time difference < 3 days)**: Choose the higher-frequency memory (高频 > 中频 > 低频)
3. **冲突确认 (Conflict confirmation, when weights are similar)**: Do not discard either side — mention both possibilities委婉ly in the response

Conflicts are marked inline: `⚠️ 可能与第N条冲突(相似度X.XX)`

**Design principle**: Risk of deleting a correct memory > cost of keeping an expired one. Never let the system unilaterally discard potentially correct memories.

### UPDATE Defense

When Mem0's `add(infer=True)` updates existing memories, it may overwrite `last_accessed_at`, corrupting the decay state. The bridge implements a snapshot-and-restore mechanism:

1. Before `add()`: snapshot all `last_accessed_at` values from Qdrant
2. Execute `add()` normally
3. After `add()`: compare and restore any `last_accessed_at` values that were changed

This ensures the decay state is never polluted by write operations.

### Performance Optimizations

| Optimization | Impact |
|-------------|--------|
| `track`/`touch` skip BGE model loading | Saves ~4-6 seconds per call |
| Batch `qdrant.retrieve` instead of N+1 queries | 100 memories: 1 roundtrip (<50ms) vs 100 roundtrips (1-3s) |
| Exponential backoff retry (1s, 2s, 4s) | Handles transient failures gracefully |
| Circuit breaker (5 failures → 120s cooldown) | Prevents hammering a down server |

## Server Script Commands

```bash
python mem0_server.py <action> [args...]

Actions:
    search <query> <user_id> [top_k] [rerank] [--no-track]  # Search (auto-tracks unless --no-track)
    add <json_messages> <user_id> <agent_id>                # Add new memory (with UPDATE defense)
    track <json_ids>                                        # Full track: last_accessed_at + access_count
    touch <json_ids>                                        # Touch only: last_accessed_at only
    get_all <user_id>                                      # Get all memories
    profile <user_id>                                      # Get user profile memories
    stats [user_id]                                        # Show access statistics
    least_used [user_id] [top_n]                           # Show least-used memories
    cleanup [--dry-run] [--threshold X] [user_id]          # Remove stale memories
```

## Troubleshooting

### FutureWarning from HuggingFace

If you see `FutureWarning: get_sentence_embedding_dimension is deprecated`, patch the embedding file:

```bash
sed -i 's/get_sentence_embedding_dimension/get_embedding_dimension/g' \
    /path/to/venv/lib/python3.11/site-packages/mem0/embeddings/huggingface.py
```

### Model Loading Warning on Every Call

This only happens once when the server starts. The model weights are cached in `~/.cache/huggingface/`.

### Qdrant Connection Failed

Ensure Qdrant is running:
```bash
curl http://localhost:6333/collections
```

If it fails, restart Qdrant:
```bash
docker run -p 6333:6333 qdrant/qdrant
```

### Offline Gap Compensation Not Triggering

Check the log output for `[mem0] Offline gap=` messages. If you don't see them after being offline > 36 hours, verify:
1. `~/.hermes/mem0_state.json` exists and has a valid `last_active` timestamp
2. System clock is correct (CMOS battery not dead)
3. The gap exceeds 36 hours (not just overnight)

## License

MIT
