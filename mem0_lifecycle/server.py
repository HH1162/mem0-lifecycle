#!/usr/bin/env python3
"""Mem0 memory lifecycle management server — reference implementation.

This script adds access frequency tracking, exponential decay scoring,
and automated cleanup of stale memories on top of the Mem0 SDK.

Usage:
    python mem0_server.py <action> [args...]

Actions:
    search <query> <user_id> [top_k] [rerank] [--no-track]  # Search memories (auto-tracks unless --no-track)
    add <json_messages> <user_id> <agent_id>                # Add new memory
    track <json_ids>                                        # Full track: update last_accessed_at + increment access_count
    touch <json_ids>                                        # Touch only: update last_accessed_at (no access_count change)
    get_all <user_id>                                      # Get all memories
    profile <user_id>                                      # Get user profile memories
    stats [user_id]                                        # Show access statistics
    least_used [user_id] [top_n]                           # Show least-used memories
    cleanup [--dry-run] [--threshold X] [user_id]          # Remove stale memories below threshold

Cleanup config:
    Default threshold: 0.05 (weighted_score)
    Half-life: 7 days
    Grace period: newly created memories (< 14 days old) are protected
    No hard protection by access count — exponential decay handles all cases
"""

import json
import os
import sys
from datetime import datetime, timezone

from mem0 import Memory
from qdrant_client import QdrantClient

# --- Tunable parameters ---

HALF_LIFE_DAYS = 7.0       # Exponential decay half-life (days)
CLEANUP_THRESHOLD = 0.05   # Memories with weighted_score < this are candidates for deletion
ACCESS_COUNT_CAP = 255     # Hard cap on access_count — prevents unbounded growth

# --- Time anchor: freezes decay while machine is offline ---

STATE_FILE = os.path.expanduser("~/.hermes/mem0_state.json")


def get_anchor_time():
    """Get the system's last-active timestamp (time anchor).

    This is the reference point for all decay calculations.
    While the machine is offline, this timestamp does not advance,
    effectively freezing memory decay during downtime.

    Includes sanity check: if the stored anchor or system clock is
    before MIN_VALID_YEAR, it is considered corrupted (e.g. CMOS
    battery failure, NTP desync) and a safe fallback is used.
    """
    MIN_VALID_YEAR = 2024

    try:
        if os.path.exists(STATE_FILE):
            state = json.loads(open(STATE_FILE).read())
            ts = state.get("last_active", "")
            if ts:
                dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.year >= MIN_VALID_YEAR:
                    return dt
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    if now.year >= MIN_VALID_YEAR:
        return now
    # Extreme fallback: system clock itself is broken (e.g. CMOS dead)
    return datetime(MIN_VALID_YEAR, 1, 1, tzinfo=timezone.utc)


def _load_state():
    """Load state from STATE_FILE, return dict."""
    try:
        if os.path.exists(STATE_FILE):
            return json.loads(open(STATE_FILE).read())
    except Exception:
        pass
    return {}

def _save_state(state):
    """Atomically save state to STATE_FILE."""
    try:
        tmp = STATE_FILE + ".tmp"
        open(tmp, 'w').write(json.dumps(state, indent=2))
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[mem0] Warning: save_state failed: {e}", file=sys.stderr)

def update_anchor_time():
    """Called on search/add to advance the time anchor.

    Only invoked when the user is actively interacting with the system.
    During downtime, this is never called, so the anchor stays frozen.

    Offline gap handling (Global Time Offset):
    If the gap between old anchor and now exceeds 36 hours (machine was offline),
    we accumulate the gap into time_offset_days. This offset is subtracted from
    effective_days during decay calculation, preventing offline time from being
    counted as "memory idle time".

    Key advantage: O(1) operation, no Qdrant writes needed, naturally idempotent.

    Example: machine off for 30 days
      raw_days = (now - last_accessed_at).days = 30
      time_offset_days = 30
      effective_days = max(0, 30 - 30) = 0 ✓
    """
    try:
        now = datetime.now(timezone.utc)
        state = _load_state()

        # Read old anchor
        old_anchor = None
        ts = state.get("last_active", "")
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.year >= 2024:
                    old_anchor = dt
            except Exception:
                pass

        # Calculate gap and accumulate offset
        if old_anchor is not None:
            gap_seconds = (now - old_anchor).total_seconds()
            gap_hours = gap_seconds / 3600

            if gap_hours > 36:
                gap_days = gap_seconds / 86400
                state['time_offset_days'] = state.get('time_offset_days', 0.0) + gap_days
                print(f"[mem0] Offline gap={gap_hours:.0f}h, accumulated offset={state['time_offset_days']:.1f} days", file=sys.stderr)

        # Update anchor to now
        state['last_active'] = now.isoformat()
        _save_state(state)

    except Exception as e:
        # Never block search/add on anchor update failure
        print(f"[mem0] Warning: update_anchor_time failed: {e}", file=sys.stderr)
        pass


def compute_weighted_score(access_count, last_accessed_iso, anchor_time=None):
    """Exponential decay based on system active time, not physical wall-clock time.

    score = min(access_count, CAP) * 0.5^(effective_days / half_life)

    effective_days = max(0, raw_days - time_offset_days)
    where raw_days = (anchor_time - last_accessed_at)
    and time_offset_days is accumulated offline gap (prevents offline snowball decay)

    When anchor_time is None, defaults to current wall-clock time.
    During cleanup/stats, anchor_time is explicitly passed to ensure
    consistent scoring across all memories in a single run.

    Timestamp parsing uses Python native fromisoformat + tzinfo check for
    robust handling of UTC, timezone-aware, naive, Z-suffix, and negative-offset
    ISO strings. On parse failure, returns 0.0 (expired) to avoid immortal zombies.

    Examples (half_life=7 days):
    - 3 accesses today -> 3.0
    - 10 accesses, last seen 21 days ago -> 1.25
    - 1 access, last seen 33 days ago -> ~0.05 (cleanup threshold)
    """
    if not access_count or not last_accessed_iso or last_accessed_iso == 'never':
        return 0.0
    try:
        ts = last_accessed_iso.replace('Z', '+00:00')
        last_dt = datetime.fromisoformat(ts)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)

        # Use anchor_time (last system activity) instead of wall-clock now()
        # This freezes decay while the machine is offline
        if anchor_time is None:
            anchor_time = get_anchor_time()
        elif anchor_time.tzinfo is None:
            anchor_time = anchor_time.replace(tzinfo=timezone.utc)

        raw_days = max(0, (anchor_time - last_dt).total_seconds() / 86400)

        # Subtract accumulated offline gap offset
        state = _load_state()
        time_offset = state.get('time_offset_days', 0.0)
        effective_days = max(0, raw_days - time_offset)

        return float(min(access_count, ACCESS_COUNT_CAP)) * (0.5 ** (effective_days / HALF_LIFE_DAYS))
    except Exception:
        # Timestamp parse failure -> treat as expired to avoid immortal zombies
        return 0.0


def get_memory_client():
    """Create Memory client with local config.

    Replace the values below with your own deployment settings.
    See https://docs.mem0.ai/quick-start for full configuration options.

    ⚡ Force offline mode — prevent transformers/hf_hub from downloading
    config files every time the model loads. Model files are already local.
    """
    import os
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    config = {
        'llm': {
            'provider': 'openai',
            'config': {
                'api_key': 'local',
                'openai_base_url': 'http://localhost:1234/v1',
                'model': 'qwen3'
            }
        },
        'embedder': {
            'provider': 'huggingface',
            'config': {
                'model': '/home/herocco/bge/bge-large-zh-v1.5'
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
    }
    return Memory.from_config(config)


def get_access_payload(qdrant, mem_id):
    """Fetch access_count and last_accessed_at for a memory from Qdrant.

    NOTE: This is kept for backward compatibility but is SLOW (N+1 queries).
    Prefer batch_get_access_payload() for bulk operations (stats, cleanup, least_used).
    """
    try:
        pt = qdrant.retrieve(collection_name='mem0', ids=[str(mem_id)], with_payload=True)
        if pt:
            p = pt[0].payload
            return p.get('access_count', 0), p.get('last_accessed_at', 'never')
    except Exception:
        pass
    return 0, 'never'


def batch_get_access_payload(qdrant, mem_ids):
    """Batch retrieve access payloads for multiple memory IDs.

    Replaces N sequential Qdrant queries with a single batch retrieve call.
    Returns dict: {mem_id: (access_count, last_accessed_at)}

    This eliminates the N+1 query problem in stats/cleanup/least_used operations.
    Without this, 100 memories = 100 network roundtrips (1-3 seconds).
    With this, 100 memories = 1 network roundtrip (<50ms).
    """
    if not mem_ids:
        return {}

    try:
        pts = qdrant.retrieve(
            collection_name='mem0',
            ids=[str(mid) for mid in mem_ids],
            with_payload=True
        )
        result = {}
        for p in pts:
            payload = p.payload or {}
            result[str(p.id)] = (
                payload.get('access_count', 0),
                payload.get('last_accessed_at', 'never')
            )
        return result
    except Exception as e:
        # CRITICAL: Never swallow Qdrant errors silently.
        # If we return {} here, cleanup will see ac=0 for ALL memories
        # and delete everything past the grace period (Fail-Deadly).
        # Raising forces the caller to abort (Fail-Safe).
        raise RuntimeError(f"Qdrant batch retrieve failed for {len(mem_ids)} memories: {e}")


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: mem0_server.py <action> [args...]"}))
        sys.exit(1)

    action = sys.argv[1]

    # Advance time anchor on user-facing actions (search/add/track/touch)
    if action in ("search", "add", "track", "touch"):
        update_anchor_time()

    try:
        # Only load Memory client for actions that need it (search, add, get_all, profile, stats, least_used, cleanup)
        # track/touch only need Qdrant — skip the expensive client init (BGE model load)
        client = None
        if action not in ("track", "touch"):
            client = get_memory_client()

        if action == "search":
            query = sys.argv[2]
            user_id = sys.argv[3]
            top_k = int(sys.argv[4]) if len(sys.argv) > 4 else 10
            rerank = sys.argv[5] == "true" if len(sys.argv) > 5 else False
            no_track = len(sys.argv) > 6 and sys.argv[6] == "--no-track"

            results = client.search(
                query=query,
                filters={'user_id': user_id},
                top_k=top_k,
                rerank=rerank,
                threshold=0.1
            )

            # Track access frequency for each matched memory (unless --no-track)
            if results and not no_track:
                results_list = results.get('results', []) if isinstance(results, dict) else results
                if results_list:
                    qdrant = QdrantClient(host='localhost', port=6333)
                    now = datetime.now(timezone.utc).isoformat()
                    for r in results_list:
                        mem_id = str(r.get('id', ''))
                        if mem_id:
                            try:
                                ac, _ = get_access_payload(qdrant, mem_id)
                                qdrant.set_payload(
                                    collection_name='mem0',
                                    payload={
                                        'access_count': min(ac + 1, ACCESS_COUNT_CAP),
                                        'last_accessed_at': now
                                    },
                                    points=[mem_id]
                                )
                            except Exception as e:
                                print(f"Warning: track access failed for {mem_id}: {e}", file=sys.stderr)

            print(json.dumps(results, default=str))

        elif action == "track":
            # Full track: update last_accessed_at AND increment access_count
            # NOTE: No Memory client needed — direct Qdrant access only
            mem_ids_json = sys.argv[2]
            mem_ids = json.loads(mem_ids_json)
            
            qdrant = QdrantClient(host='localhost', port=6333)
            now = datetime.now(timezone.utc).isoformat()
            
            # Batch retrieve all payloads first (avoids N+1 queries)
            payload_map = {}
            try:
                pts = qdrant.retrieve(
                    collection_name='mem0',
                    ids=[str(mid) for mid in mem_ids],
                    with_payload=True
                )
                for p in pts:
                    payload = p.payload or {}
                    payload_map[str(p.id)] = payload.get('access_count', 0)
            except Exception as e:
                print(f"Warning: batch retrieve failed in track: {e}", file=sys.stderr)
            
            tracked = 0
            for mem_id in mem_ids:
                try:
                    ac = payload_map.get(str(mem_id), 0)
                    qdrant.set_payload(
                        collection_name='mem0',
                        payload={
                            'access_count': min(ac + 1, ACCESS_COUNT_CAP),
                            'last_accessed_at': now
                        },
                        points=[str(mem_id)]
                    )
                    tracked += 1
                except Exception as e:
                    print(f"Warning: track failed for {mem_id}: {e}", file=sys.stderr)
            
            print(json.dumps({"result": f"Tracked {tracked} memories"}))

        elif action == "touch":
            # Touch only: update last_accessed_at, do NOT change access_count
            # Used for shadow memories (pure duplicates that should decay naturally)
            # NOTE: No Memory client needed — direct Qdrant access only
            mem_ids_json = sys.argv[2]
            mem_ids = json.loads(mem_ids_json)
            
            qdrant = QdrantClient(host='localhost', port=6333)
            now = datetime.now(timezone.utc).isoformat()
            touched = 0
            for mem_id in mem_ids:
                try:
                    qdrant.set_payload(
                        collection_name='mem0',
                        payload={'last_accessed_at': now},
                        points=[str(mem_id)]
                    )
                    touched += 1
                except Exception as e:
                    print(f"Warning: touch failed for {mem_id}: {e}", file=sys.stderr)
            
            print(json.dumps({"result": f"Touched {touched} memories"}))

        elif action == "add":
            messages_json = sys.argv[2]
            user_id = sys.argv[3]
            agent_id = sys.argv[4]

            messages = json.loads(messages_json)

            # UPDATE DEFENSE: Snapshot access payloads BEFORE add()
            # Mem0's infer=True may update existing memories, potentially overwriting
            # last_accessed_at. We capture the state before and restore after.
            qdrant = QdrantClient(host='localhost', port=6333)
            pre_add_payloads = {}
            try:
                points, _ = qdrant.scroll(collection_name='mem0', limit=1000, with_payload=True)
                for pt in points:
                    payload = pt.payload or {}
                    if 'last_accessed_at' in payload and payload['last_accessed_at'] != 'never':
                        pre_add_payloads[str(pt.id)] = payload['last_accessed_at']
            except Exception as e:
                print(f"Warning: failed to snapshot payloads before add: {e}", file=sys.stderr)

            client.add(messages, user_id=user_id, agent_id=agent_id)

            # Restore last_accessed_at for any existing memories that were modified
            restored = 0
            if pre_add_payloads:
                try:
                    points, _ = qdrant.scroll(collection_name='mem0', limit=1000, with_payload=True)
                    current_ids = {str(pt.id): pt.payload or {} for pt in points}
                    for mem_id, original_la in pre_add_payloads.items():
                        if mem_id in current_ids:
                            current_la = current_ids[mem_id].get('last_accessed_at', '')
                            if current_la != original_la and current_la != 'never':
                                # last_accessed_at was changed by add() — restore it
                                qdrant.set_payload(
                                    collection_name='mem0',
                                    payload={'last_accessed_at': original_la},
                                    points=[mem_id]
                                )
                                restored += 1
                except Exception as e:
                    print(f"Warning: failed to restore payloads after add: {e}", file=sys.stderr)

            if restored > 0:
                print(f"[mem0] UPDATE DEFENSE: restored last_accessed_at for {restored} memories", file=sys.stderr)
            print(json.dumps({"result": "Fact stored."}))

        elif action == "get_all":
            user_id = sys.argv[2]
            memories = client.get_all(filters={'user_id': user_id})
            print(json.dumps(memories))

        elif action == "profile":
            user_id = sys.argv[2]
            memories = client.get_all(filters={'user_id': user_id})
            if isinstance(memories, dict):
                memories = memories.get("results", [])
            lines = [m.get("memory", "") for m in memories if m.get("memory")]
            print(json.dumps({
                "result": "\n".join(lines),
                "count": len(lines)
            }))

        elif action == "stats":
            user_id = sys.argv[2] if len(sys.argv) > 2 else "example-user"
            qdrant = QdrantClient(host='localhost', port=6333)

            # Use time anchor for consistent scoring (not wall-clock now)
            anchor = get_anchor_time()

            all_mems = client.get_all(filters={'user_id': user_id})
            results = all_mems.get('results', []) if isinstance(all_mems, dict) else []

            # BATCH retrieve all payloads in one call (eliminates N+1 query problem)
            mem_ids = [str(m.get('id', '')) for m in results if m.get('id')]
            payload_map = batch_get_access_payload(qdrant, mem_ids)

            total = len(results)
            with_access = 0
            never_accessed = 0
            max_access = 0
            total_access = 0
            total_weighted = 0.0
            max_weighted = 0.0

            for m in results:
                mem_id = str(m.get('id', ''))
                ac, la = payload_map.get(mem_id, (0, 'never'))
                total_access += ac
                if ac > 0:
                    with_access += 1
                else:
                    never_accessed += 1
                if ac > max_access:
                    max_access = ac

                w = compute_weighted_score(ac, la, anchor_time=anchor)
                total_weighted += w
                max_weighted = max(max_weighted, w)

            avg_access = total_access / with_access if with_access > 0 else 0
            avg_weighted = total_weighted / with_access if with_access > 0 else 0

            print(json.dumps({
                'total_memories': total,
                'never_accessed': never_accessed,
                'with_access': with_access,
                'avg_access_raw': round(avg_access, 2),
                'max_access_raw': max_access,
                'total_accesses': total_access,
                'avg_weighted_score': round(avg_weighted, 4),
                'max_weighted_score': round(max_weighted, 4),
                'half_life_days': int(HALF_LIFE_DAYS)
            }))

        elif action == "least_used":
            user_id = sys.argv[2] if len(sys.argv) > 2 else "example-user"
            top_n = int(sys.argv[3]) if len(sys.argv) > 3 else 10
            qdrant = QdrantClient(host='localhost', port=6333)

            anchor = get_anchor_time()

            all_mems = client.get_all(filters={'user_id': user_id})
            results = all_mems.get('results', []) if isinstance(all_mems, dict) else []

            # BATCH retrieve all payloads in one call (eliminates N+1 query problem)
            mem_ids = [str(m.get('id', '')) for m in results if m.get('id')]
            payload_map = batch_get_access_payload(qdrant, mem_ids)

            with_stats = []
            for m in results:
                mem_id = str(m.get('id', ''))
                ac, la = payload_map.get(mem_id, (0, 'never'))
                w = compute_weighted_score(ac, la, anchor_time=anchor)
                with_stats.append({
                    'id': mem_id,
                    'memory': m.get('memory', '')[:120],
                    'access_count': ac,
                    'last_accessed_at': la,
                    'weighted_score': round(w, 4),
                    'created_at': m.get('created_at', '')
                })

            with_stats.sort(key=lambda x: x['weighted_score'])
            print(json.dumps(with_stats[:top_n], indent=2))

        elif action == "cleanup":
            # Parse arguments: cleanup [--dry-run] [--threshold X] [user_id]
            args = sys.argv[2:]
            dry_run = '--dry-run' in args
            user_id = 'example-user'
            threshold = CLEANUP_THRESHOLD

            skip_next = False
            for i, arg in enumerate(args):
                if skip_next:
                    skip_next = False
                    continue

                if arg == '--threshold' and i + 1 < len(args):
                    try:
                        threshold = float(args[i + 1])
                        skip_next = True  # Mark value as consumed so it won't become user_id
                    except ValueError:
                        pass
                elif not arg.startswith('-'):
                    user_id = arg

            qdrant = QdrantClient(host='localhost', port=6333)

            anchor = get_anchor_time()

            all_mems = client.get_all(filters={'user_id': user_id})
            results = all_mems.get('results', []) if isinstance(all_mems, dict) else []

            # BATCH retrieve all payloads in one call (eliminates N+1 query problem)
            # Circuit breaker: if Qdrant is down, abort cleanup entirely (Fail-Safe).
            mem_ids = [str(m.get('id', '')) for m in results if m.get('id')]
            try:
                payload_map = batch_get_access_payload(qdrant, mem_ids)
            except RuntimeError as e:
                print(json.dumps({
                    "error": str(e),
                    "action": "ABORT_CLEANUP_TO_PREVENT_MASS_DELETION"
                }))
                sys.exit(1)

            candidates = []
            kept = []

            for m in results:
                mem_id = str(m.get('id', ''))
                ac, la = payload_map.get(mem_id, (0, 'never'))
                w = compute_weighted_score(ac, la, anchor_time=anchor)

                # Grace period: newly created memories (< 14 days old) are protected.
                # Uses anchor_time as single source of truth instead of datetime.now()
                # to remain resilient against system clock drift (NTP desync, CMOS failure).
                grace_protected = False
                created_at = m.get('created_at', '')
                if created_at:
                    try:
                        ca_ts = created_at.replace('Z', '+00:00')
                        ca_dt = datetime.fromisoformat(ca_ts)
                        if ca_dt.tzinfo is None:
                            ca_dt = ca_dt.replace(tzinfo=timezone.utc)
                        days_old = (anchor - ca_dt).total_seconds() / 86400
                        grace_protected = days_old < 14
                    except Exception:
                        # If timestamp parsing fails, default to protecting the memory
                        # to avoid accidentally deleting newly created entries
                        grace_protected = True

                entry = {
                    'id': mem_id,
                    'memory': m.get('memory', '')[:120],
                    'access_count': ac,
                    'weighted_score': round(w, 4),
                    'last_accessed_at': la,
                    'grace_protected': grace_protected
                }

                if w < threshold and not grace_protected:
                    candidates.append(entry)
                else:
                    entry['reason'] = 'grace_period' if grace_protected else f'score {w:.4f} >= threshold {threshold}'
                    kept.append(entry)

            # Sort candidates by weighted score (lowest first)
            candidates.sort(key=lambda x: x['weighted_score'])

            if dry_run:
                output = {
                    'mode': 'DRY_RUN',
                    'threshold': threshold,
                    'half_life_days': int(HALF_LIFE_DAYS),
                    'to_delete': len(candidates),
                    'kept': len(kept),
                    'candidates': candidates
                }
                print(json.dumps(output, indent=2))
            else:
                # Delete via Mem0 SDK (synchronizes both metadata layer AND Qdrant vector store)
                deleted_ids = []
                failed_ids = []
                for c in candidates:
                    try:
                        client.delete(memory_id=c['id'])
                        deleted_ids.append(c['id'])
                    except Exception as e:
                        failed_ids.append({'id': c['id'], 'error': str(e)})

                output = {
                    'mode': 'EXECUTED',
                    'threshold': threshold,
                    'deleted_count': len(deleted_ids),
                    'deleted_ids': deleted_ids,
                    'failed': failed_ids,
                    'kept_count': len(kept)
                }
                print(json.dumps(output, indent=2))

        else:
            print(json.dumps({"error": f"Unknown action: {action}"}))
            sys.exit(1)

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
