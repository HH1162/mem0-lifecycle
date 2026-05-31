# Mem0 Memory Enhancement via Forgetting Curve Model

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
pip install git+https://github.com/HH1162/mem0-lifecycle.git
```

Or clone and install locally:

```bash
git clone https://github.com/HH1162/mem0-lifecycle.git
cd mem0-lifecycle
pip install -e .
```

## Performance Optimizations (v0.1.1+)

This plugin includes critical performance fixes for production deployments:

**1. Async startup (zero Gateway delay):**
The cleanup script uses `systemd-run --scope` to run in background, so Gateway starts instantly. Cleanup runs asynchronously without blocking.

**2. Batch Qdrant queries (N+1 eliminated):**
All stats/cleanup operations use single batch retrieve instead of per-memory queries. 100 memories = 1 network call (<50ms) instead of 100 calls (1-3s).

**3. Embedding model cold start:**
Since this is a bridge layer, you only pay the embedding model load cost once per process. Combined with async execution, this never blocks your main application.

See [Performance FAQ](#performance-faq) below for details.

## Usage

### Quick Start

Replace your existing Mem0 client initialization with our lifecycle-aware server:

```python
from mem0_lifecycle import Mem0LifecycleServer

# Initialize with your Mem0 config
lifecycle_server = Mem0LifecycleServer(
    config={
        'llm': {
            'provider': 'openai',
            'config': {
                'api_key': 'your-api-key-here',
                'openai_base_url': 'http://localhost:1234/v1',
                'model': 'qwen3'
            }
        },
        'embedder': {
            'provider': 'huggingface',
            'config': {
                'model': '/path/to/embedding-model'
            }
        },
        'vector_store': {
            'provider': 'qdrant',
            'config': {
                'collection_name': 'mem0',
                'embedding_model_dims': 1024,
                'host': 'localhost',
                'port': 6333
            }
        },
        'graph_store': {
            'provider': 'redis',
            'config': {
                "username": "default",
                "password": "your-redis-password",
                "host": "localhost",
                "port": 6379
            }
        }
    }
)

# Search automatically tracks access frequency
results = lifecycle_server.search("query text", user_id="hermes-user")

# Cleanup stale memories
lifecycle_server.cleanup(dry_run=False)
```

### Command Line Interface

```bash
# Search (auto-tracks access frequency)
mem0-server search "query text" hermes-user 5 true

# View statistics
mem0-server stats hermes-user

# Find least-used memories
mem0-server least_used hermes-user 10

# Cleanup stale memories
mem0-server cleanup --dry-run hermes-user   # Preview
mem0-server cleanup hermes-user             # Execute
```

### Automation via systemd

Create a drop-in config at `$SYSTEMD_USER_DIR/your-gateway.service.d/mem0-cleanup.conf`:

```ini
[Service]
ExecStartPre=/path/to/mem0-daily-cleanup.sh
TimeoutStartSec=300
```

The cleanup script uses a date marker (`/tmp/.mem0_cleanup_date`) to ensure cleanup runs at most once per day, even on service restarts.

## Configuration

### Tunable Parameters

All parameters are configurable in `mem0_lifecycle.decay` module:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `HALF_LIFE_DAYS` | 7.0 | Exponential decay half-life (days) |
| `CLEANUP_THRESHOLD` | 0.05 | Memories below this weighted score are candidates for deletion |
| `ACCESS_COUNT_CAP` | 255 | Hard cap preventing unbounded growth |
| `GRACE_PERIOD_DAYS` | 14 | New memories protected from deletion |

Override these in your code:

```python
from mem0_lifecycle import decay

decay.HALF_LIFE_DAYS = 14.0       # Longer retention period
decay.CLEANUP_THRESHOLD = 0.01     # More aggressive cleanup
decay.GRACE_PERIOD_DAYS = 7        # Shorter protection for new memories
```

## Production Results

Deployed under Hermes Agent with local Mem0 v2.0.4 + Qdrant + bge-large-zh-v1.5 embedding:

- Active memories stabilized at ~20 entries (down from 30+ before cleanup)
- Zero false deletions across multiple cleanup cycles
- Zero manual intervention required — system fully self-manages
- Single-access memory auto-cleaned after ~33 days of inactivity
- Max-count memory auto-cleaned after ~86 days of inactivity
- Retrieval quality improved measurably — fewer stale entries pollute top-k results

## Comparison with Alternatives

| Approach | Problem | Our Solution |
|----------|---------|--------------|
| Hard TTL (delete after N days) | Kills useful memories abruptly regardless of usage pattern | Decay lets frequently-accessed memories outlive idle ones |
| Simple LRU (delete least-recently-used) | Ignores frequency — a once-accessed old memory ranks same as a never-accessed new one | Weighted score combines both frequency AND recency |
| Manual cleanup | Requires user intervention — defeats autonomous agents | Fully automated, zero-touch operation |
| Vector similarity pruning | Removes semantically redundant entries but keeps factually-stale ones | Targets actual usage patterns, not semantic overlap |

## Design Philosophy

This implementation follows these principles:

1. **Bridge layer architecture** — No core Mem0 modification required. Works as a drop-in replacement.
2. **Defensive programming** — Timestamp parse failures default to safe states (score=0.0 for unknown timestamps, protected status for grace period failures).
3. **Dual-layer consistency** — Cleanup properly deletes from both Qdrant vector index AND Mem0 metadata layer to prevent orphaned entries.
4. **Idempotent automation** — Daily date markers prevent redundant cleanup runs within the same day.

## References

- Mem0 Issue: [mem0ai/mem0#5330](https://github.com/mem0ai/mem0/issues/5330) — Feature proposal for native memory lifetime management
- Hermes Agent PR: [NousResearch/hermes-agent#35870](https://github.com/NousResearch/hermes-agent/pull/35870) — Original plugin submission (closed per maintainer guidance to use standalone repo)
- Ebbinghaus Forgetting Curve: Classic research on human memory decay patterns

## Vibe Coding Declaration

This plugin was developed through iterative LLM-assisted coding sessions (Vibe Coding). The exponential decay model, safety guards (grace period, access count cap, parse failure defaults), and systemd automation went through 5 rounds of architectural review covering edge cases: timezone handling, stale zombie prevention, metadata consistency between vector and index layers, and integer overflow protection. All validated through local production deployment before submission.

## Performance FAQ

**Q: Will this slow down my Gateway startup?**
A: No. The cleanup script uses `systemd-run --scope` to run asynchronously. Gateway starts instantly; cleanup happens in background without blocking.

**Q: Why does cleanup take 3-10 seconds on first run?**
A: Embedding model cold start (bge-large-zh-v1.5 = 1.3GB). This only happens once per process. Subsequent runs are <500ms. With async execution, this never blocks your application.

**Q: How many Qdrant queries does stats/cleanup make?**
A: One. We use batch retrieve instead of N+1 per-memory queries. 100 memories = 1 network call instead of 100.

**Q: Can I tune the half-life or threshold?**
A: Yes. All parameters in `mem0_lifecycle.decay` are configurable at runtime before calling server methods. See [Configuration](#configuration) section.
