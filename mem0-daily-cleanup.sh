#!/bin/bash
# Mem0 daily cleanup — runs on gateway startup, once per day.
# Usage as systemd ExecStartPre in your gateway service config.

MARKER="/tmp/.mem0_cleanup_date"
TODAY=$(date +%Y-%m-%d)
CLEANED=$(cat "$MARKER" 2>/dev/null)

if [ "$TODAY" != "$CLEANED" ]; then
    if cd /path/to/mem0 && python mem0_server.py cleanup | systemd-cat -t mem0-cleanup; then
        echo "$TODAY" > "$MARKER"
    else
        logger -t mem0-cleanup "Cleanup failed, will retry next gateway start"
    fi
fi

exit 0
