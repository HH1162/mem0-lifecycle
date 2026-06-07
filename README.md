# Mem0 Memory Lifecycle — Auto-Cleanup with Ebbinghaus Decay

> A production-tested memory lifecycle system that makes Mem0 behave like human memory — frequently accessed information persists, unused information naturally decays.

## The Problem

When running Mem0 locally, memories accumulate indefinitely. There is no expiration, no decay, no distinction between a preference retrieved daily and a temporary note created once and forgotten. Over time:

- **Retrieval noise compounds** — top-k results mix useful facts with stale artifacts, inflating context tokens and introducing contradictions
- **Stale memories pollute answers** — outdated preferences and expired task progress logs show up alongside current facts
- **Zero user intervention** — no built-in mechanism exists to address this without modifying Mem0 core

This project solves that problem by implementing an **exponential decay model inspired by the Ebbinghaus forgetting curve**, sitting as a bridge layer between your application and Mem0. No core Mem0 modification required.

## How It Works

### The Forgetting Curve Model

```python
weighted_score = min(access_count, 255) * 0.5 ** (days_since_last_access / 7)
```

This formula mirrors how human memory consolidation works:

| Mechanism | Biology | Implementation |
|-----------|---------|----------------|
| **Repetition strengthens memory** | Repeated exposure reinforces neural pathways | `access_count` incremented on every search hit |
| **Time weakens memory** | Unused connections naturally fade | Exponential decay halves score every 7 days |
| **Diminishing returns on repetition** | After enough exposure, additional repetition adds little | `access_count` capped at 255 |
| **Recent events are protected** | New memories need consolidation time | 14-day grace period for newly created entries |
| **Below threshold = forgotten** | When a memory trace becomes too weak, it's lost | Score < 0.05 triggers deletion |

### Architecture

```
Hermes Agent ←→ mem0_lifecycle (bridge) ←→ Mem0 SDK ←→ Qdrant
                           ↓
       Access tracking + decay scoring + automated cleanup
```

The bridge intercepts search calls, tracks access frequency in Qdrant payloads, computes decay scores, and automates cleanup — all transparent to the calling application.

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
| `GAP_THRESHOLD` | 36 hours | Offline duration that triggers timestamp shift |

### Time Anchor System

To prevent offline time from being counted as "memory idle time", this implementation uses a **time anchor** that freezes while the machine is offline:

- **Anchor persistence**: Stored in `~/.hermes/mem0_state.json` (survives restarts)
- **Gap compensation**: When offline > 36 hours, all `last_accessed_at` timestamps are shifted forward by the gap duration before updating the anchor
- **Mathematical equivalence**: Preserves relative distance between anchor and last_accessed_at, preventing offline time from inflating effective_days

Example: Machine off for 30 days
- Before: `last_accessed_at=Day0, anchor=Day0` → effective_days=0
- After: `last_accessed_at=Day30, anchor=Day30` → effective_days=0 ✓

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

### Deduplication

The bridge includes optional deduplication to prevent redundant memories from being injected into prompts:

- **Embedding Cosine Similarity**: Uses vector similarity (>0.85 threshold) to identify duplicate memories
- **Time-based priority**: When duplicates are found, the most recently updated memory is kept
- **Over-fetching**: Retrieves top_k=20 from Qdrant, deduplicates, then returns top 5 to maximize recall

### Conflict Resolution

When conflicting memories are detected:

1. Both memories are injected with timestamps
2. LLM is instructed to prioritize the most recent memory
3. Format: `[Updated N days ago | YYYY-MM-DD] Memory text`

## Performance

- **Search overhead**: <20ms for top_k=20 retrieval + deduplication
- **Cleanup overhead**: O(N) where N is total memories, typically <100ms for <1000 memories
- **Memory usage**: Minimal, only stores access metadata in Qdrant payloads

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

## License

MIT
