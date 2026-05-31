# Mem0 Memory Lifecycle Management — Reference Implementation

> Production-tested bridge layer that adds memory lifetime management to Mem0.  
> Shared as reference for [mem0ai/mem0#5330](https://github.com/mem0ai/mem0/issues/5330) — Proposal: Memory access frequency tracking and lifetime-based cleanup.

## Core Formula

```python
weighted_score = min(access_count, 255) × 0.5^(days_since_last_access / 7)
```

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Half-life | 7 days | Balances retention vs. decay |
| Cleanup threshold | 0.05 | Memories below this score → candidates for deletion |
| Access count cap | 255 | Prevents unbounded growth; 255-access memory decays below threshold in ~86 days |
| Grace period | 14 days | New memories cannot be deleted regardless of score |

## Features

- **Access frequency tracking**: Auto-increment on every `search` hit, stored in Qdrant payload
- **Exponential decay scoring**: Score halves every 7 days of inactivity
- **Grace period protection**: Memories < 14 days old are protected from deletion
- **Safe-by-default**: Timestamp parse failures default to protection, never deletion
- **Automated cleanup**: Runs via systemd `ExecStartPre` with daily guard

## Files

| File | Purpose |
|------|---------|
| `mem0_server.py` | Full reference implementation: search tracking, stats, cleanup |
| `mem0-daily-cleanup.sh` | Shell wrapper for daily automated cleanup |

## systemd Integration

Create a drop-in config at `/etc/systemd/system/your-gateway.service.d/mem0-cleanup.conf`:

```ini
[Service]
ExecStartPre=/path/to/mem0-daily-cleanup.sh
TimeoutStartSec=300
```

Then run `systemctl daemon-reload`.

## Real-World Results

After running in production:
- ~20 active memories, access counts range 0–2
- Zero false deletions
- Zero manual intervention needed
- Single-access memory auto-cleaned after ~33 days
- Max-count memory (255) auto-cleaned after ~86 days of inactivity
