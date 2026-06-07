# Mem0 Agentic Enhancement Plugin

> Two production-tested optimizations for Mem0 when used as an agent's long-term memory: **(1) old/new memory conflict resolution** and **(2) stale memory cleanup via the Ebbinghaus forgetting curve**.

## Why This Exists

When Mem0 is used as an agent's persistent memory, two fundamental problems emerge that the stock Mem0 SDK does not address:

### Problem 1: Old/New Memory Conflicts

When an agent updates a fact (e.g., "I now prefer dark mode"), both the old memory ("User prefers light mode") and the new one can simultaneously rank in the top-k results. The agent sees contradictory information. Worse, the old memory keeps getting "accidentally retrieved" on every search, so its access count keeps growing — making it **immortal** and resistant to any cleanup mechanism.

**This plugin solves it with:**
- Over-fetching (top_k=20) + text-based deduplication to catch conflicting memories before injection
- Dual-threshold classification: exact duplicates are shadowed; near-duplicates are flagged for the LLM
- Three-layer conflict resolution rules injected into the system prompt so the LLM knows how to adjudicate
- Shadow IDs receive only `touch` (timestamp update), never `track` (count increment), preventing immortal zombies

### Problem 2: Stale Memory Accumulation

Mem0 has no expiration. Memories created once and never used again pile up indefinitely, polluting search results and inflating context tokens. A simple "delete old memories" approach fails because machine downtime looks like "memory idle time" — turning off your computer for a weekend would trigger mass deletions.

**This plugin solves it with:**
- Exponential decay scoring inspired by the Ebbinghaus forgetting curve
- Access frequency tracking on every search hit — frequently used memories stay strong
- Time anchor system that freezes decay while the machine is offline
- Global time offset (v7) that compensates for extended downtime without touching the database
- Automated daily cleanup of memories whose weighted score falls below threshold

---

## Architecture

```
Agent ←→ Mem0 Plugin (bridge) ←→ Mem0 SDK ←→ Qdrant
                    ↓
   Conflict resolution + Over-fetching + Dedup + Access tracking + Decay scoring + Cleanup
```

---

## Feature 1: Memory Conflict Resolution

### How It Works

**Step 1: Over-Fetching**

Instead of retrieving top_k=5 directly, we fetch top_k=20 from Qdrant. This gives us enough context to identify duplicates and conflicts before injecting anything into the prompt.

**Step 2: Hash Pre-Scan + Dual-Threshold Deduplication**

**Phase 1 — Hash Pre-Scan (O(N) exact dedup)**: Before any cosine comparison, each memory text is normalized (strip punctuation, collapse whitespace, lowercase) and hashed with MD5. Hash collisions are marked as **shadow** immediately — no cosine computation needed. This completely eliminates false negatives from punctuation/whitespace/case differences (e.g., "dark theme!" vs "dark theme.").

**Timestamp backfill**: If a shadow memory has a newer `updated_at` than the retained entry, the retained entry's timestamp is backfilled so subsequent conflict resolution sees the latest state.

**Phase 2 — Cosine Similarity (O(N²) semantic dedup)**: Only non-shadowed memories enter the cosine loop with `bge-large-zh-v1.5` embeddings:

| Cosine | Action |
|--------|--------|
| > 0.92 | **HIGH conflict** — only track winner, freeze loser |
| 0.75 – 0.92 | Run **Config Check** (entity extraction) for promotion |
| < 0.75 | Normal injection, no conflict |

**Config Check with Entity Type Tiering**: When cosine is in the 0.75–0.92 range, entity triples (IP, port, version, URL, path) are extracted and compared:
- **endpoint** (IP/Port/URL): Always promoted to HIGH — these are globally unique
- **version/path/hostname**: Promoted to HIGH only if cosine ≥ 0.85 — allows multi-version coexistence (e.g., Python 3.9 and 3.11)
- **other**: Remains MEDIUM — both tracked symmetrically

**Critical**: Qdrant's original similarity order is preserved. We never re-sort by `updated_at` — that would inject irrelevant memories just because they're recent.

After deduplication, only top 5 are injected into the prompt.

**Step 3: Shadow Protection**

Shadow IDs (exact duplicates) receive `touch` only — their `last_accessed_at` is updated but `access_count` is NOT incremented. This prevents them from becoming immortal. They will eventually decay and be cleaned up naturally.

**Step 4: Frequency Labels**

Each injected memory carries a frequency label visible to the LLM:

```
[Updated 2 days ago | 2026-06-05 | High freq] User prefers dark mode
[Updated 45 days ago | 2026-04-21 | Low freq] ⚠️ May conflict with #1 (similarity 0.91) User prefers light mode
```

Frequency categories:
- **High freq** (>20 accesses): Important fact, prioritize trust
- **Mid freq** (5–20 accesses): Regular information
- **Low freq** (<5 accesses): May be outdated

**Step 5: LLM Conflict Resolution Rules**

Three-layer rules are injected into the system prompt:

1. **Recency first**: Choose the more recently updated memory
2. **Frequency first** (when time difference < 3 days): Choose the higher-frequency memory
3. **Conflict confirmation** (when weights are similar): Do not discard either side — mention both possibilities in the response

**Design principle**: Risk of deleting a correct memory > cost of keeping an expired one. The system never unilaterally discards potentially correct memories.

---

## Feature 2: Stale Memory Cleanup via Forgetting Curve

### The Decay Model

```python
weighted_score = min(access_count, 255) * 0.5 ** (effective_days / 7)
effective_days = max(0, raw_days - time_offset_days)
```

This mirrors human memory consolidation:

| Mechanism | Biology | Implementation |
|-----------|---------|----------------|
| Repetition strengthens memory | Repeated exposure reinforces neural pathways | `access_count` incremented on every search hit |
| Time weakens memory | Unused connections naturally fade | Exponential decay halves score every 7 days |
| Diminishing returns | After enough exposure, additional repetition adds little | `access_count` capped at 255 |
| Recent events protected | New memories need consolidation time | 14-day grace period |
| Below threshold = forgotten | When a memory trace becomes too weak, it's lost | Score < 0.05 triggers deletion |

### Decay Curve (half_life = 7 days)

| Access Count | Day 0 | Day 7 | Day 14 | Day 21 | Day 28 | Days to Deletion |
|-------------|-------|-------|--------|--------|--------|-----------------|
| 1 (low) | 1.0 | 0.5 | 0.25 | 0.125 | 0.063 | ~33 days |
| 5 (mid) | 5.0 | 2.5 | 1.25 | 0.625 | 0.313 | ~47 days |
| 20 (high) | 20 | 10 | 5 | 2.5 | 1.25 | ~61 days |
| 255 (capped) | 255 | 127 | 63.8 | 31.9 | 15.9 | ~86 days |

**Key insight**: High-frequency memories take ~86 days to decay to deletion threshold, not the intuitive 50 days.

### Time Anchor System

To prevent machine downtime from being counted as "memory idle time":

- **Anchor persistence**: Stored in `~/.hermes/mem0_state.json` (survives restarts)
- **Global time offset (v7)**: When offline > 36 hours, the gap is accumulated into `time_offset_days`. During decay calculation, `effective_days = max(0, raw_days - time_offset_days)`. This is an O(1) operation — no database writes needed, naturally idempotent, infinitely scalable.

Example: Machine off for 30 days
```
raw_days = (now - last_accessed_at).days = 30
time_offset_days = 30
effective_days = max(0, 30 - 30) = 0 ✓
```

### Search/Track Decoupling

Three distinct operations:

| Command | Updates Timestamp | Increments Count | Use Case |
|---------|------------------|-----------------|----------|
| `search --no-track` | No | No | Read-only queries |
| `track` | Yes | Yes | Injected memories (after dedup) |
| `touch` | Yes | No | Shadow memories (pure duplicates) |

### UPDATE Defense

When Mem0's `add(infer=True)` updates existing memories, it may overwrite `last_accessed_at`, corrupting the decay state. We implement snapshot-and-restore:

1. Before `add()`: snapshot all `last_accessed_at` values
2. Execute `add()`
3. After `add()`: compare and restore any changed values

---

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HALF_LIFE_DAYS` | 7 | Half-life period for decay |
| `CLEANUP_THRESHOLD` | 0.05 | Minimum weighted score to keep |
| `ACCESS_COUNT_CAP` | 255 | Maximum access count |
| `GRACE_PERIOD` | 14 days | Memories created within grace period are protected |
| `GAP_THRESHOLD` | 36 hours | Offline duration that triggers time offset |
| `TOP_K_FETCH` | 20 | Over-fetch count before deduplication |
| `TOP_K_INJECT` | 5 | Final count injected into prompt |

---

## Directory Configuration

This plugin requires three directories to be configured via environment variables or `mem0.json`:

### 1. Mem0 Server Directory (`<mem0-dir>`)

Where you cloned and set up the Mem0 server:

```bash
# Set via environment variable
export MEM0_SERVER="<mem0-dir>/mem0_server.py"

# Or in ~/.hermes/mem0.json
{
  "mem0_server": "<mem0-dir>/mem0_server.py"
}
```

**What's inside `<mem0-dir>`:**
- `mem0_server.py` — the custom server script with decay, track, touch commands
- `.venv/` — Python virtual environment for Mem0 dependencies

### 2. Mem0 Python Interpreter (`<mem0-venv>`)

The Python executable from the Mem0 virtual environment:

```bash
# Set via environment variable
export MEM0_PYTHON="<mem0-venv>/bin/python3"

# Or in ~/.hermes/mem0.json
{
  "mem0_python": "<mem0-venv>/bin/python3"
}
```

**Note**: This must be the Python inside the `.venv/` directory where Mem0 is installed, not your system Python.

### 3. Embedder Model Directory (`<model-path>`)

Path to the local BGE embedding model (or use a model name from HuggingFace):

```bash
# Set via environment variable
export EMBEDDER_MODEL="<model-path>/bge-large-zh-v1.5"

# Or in ~/.hermes/mem0.json
{
  "embedder_model": "<model-path>/bge-large-zh-v1.5"
}
```

**Note**: If you use a model name (e.g., `"bge-large-zh-v1.5"`), it will be downloaded from HuggingFace on first use. For offline mode, provide the full local path.

### Example Configuration

```json
// ~/.hermes/mem0.json
{
  "mode": "local",
  "mem0_server": "/path/to/mem0/mem0_server.py",
  "mem0_python": "/path/to/mem0/.venv/bin/python3",
  "embedder_model": "/path/to/models/bge-large-zh-v1.5",
  "llm_base_url": "http://localhost:1234/v1",
  "llm_model": "qwen3",
  "embedding_dims": 1024,
  "qdrant_host": "localhost",
  "qdrant_port": 6333
}
```

---

## Installation

```bash
pip install git+https://github.com/HH1162/mem0-agentic-enhancement-plugin.git
```

---

## Server Script Commands

```bash
python mem0_server.py <action> [args...]

Actions:
    search <query> <user_id> [top_k] [rerank] [--no-track]  # Search (auto-tracks unless --no-track)
    add <json_messages> <user_id> <agent_id>                # Add new memory (with UPDATE defense)
    track <json_ids>                                        # Full track: timestamp + count
    touch <json_ids>                                        # Touch only: timestamp only
    stats [user_id]                                         # Show access statistics
    least_used [user_id] [top_n]                            # Show least-used memories
    cleanup [--dry-run] [--threshold X] [user_id]           # Remove stale memories
```

---

## Performance

| Operation | Overhead |
|-----------|----------|
| Search + deduplication (top_k=20 → 5) | <25ms |
| Track (batch retrieve + update) | <50ms per batch |
| Touch | <10ms per ID |
| Cleanup (1000 memories) | <100ms |
| BGE model load (skipped for track/touch) | Saved ~4–6 seconds per call |

---

## Troubleshooting

### FutureWarning from HuggingFace

If you see `FutureWarning: get_sentence_embedding_dimension is deprecated`:

```bash
sed -i 's/get_sentence_embedding_dimension/get_embedding_dimension/g' \
    /path/to/venv/lib/python3.11/site-packages/mem0/embeddings/huggingface.py
```

### Model Loading Warning

Only happens once when the server starts. Model weights are cached in `~/.cache/huggingface/`.

### Qdrant Connection Failed

```bash
curl http://localhost:6333/collections
```

If it fails, restart Qdrant:
```bash
docker run -p 6333:6333 qdrant/qdrant
```

### Offline Gap Compensation Not Triggering

Check stderr logs for `[mem0] Offline gap=` messages. Verify:
1. `~/.hermes/mem0_state.json` exists with valid `last_active`
2. System clock is correct
3. Gap exceeds 36 hours

---

## License

MIT
