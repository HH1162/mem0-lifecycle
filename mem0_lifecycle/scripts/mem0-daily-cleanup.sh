#!/bin/bash
# Mem0 daily cleanup: async background execution via systemd-run.
# Gateway starts instantly; cleanup runs in background without blocking.
# Only runs once per day (date marker: /tmp/.mem0_cleanup_date).

MARKER="/tmp/.mem0_cleanup_date"
TODAY=$(date +%Y-%m-%d)
CLEANED=$(cat "$MARKER" 2>/dev/null)

if [ "$TODAY" = "$CLEANED" ]; then
    # Already cleaned today — skip
    exit 0
fi

# Fire-and-forget: spawn cleanup in a background scope.
# Gateway continues starting immediately regardless of cleanup outcome.
systemd-run --user --scope --unit=mem0-cleanup-bg /bin/bash -c "
    cd /media/data/mem0 && source .venv/bin/activate && python mem0_server.py cleanup 2>&1 | systemd-cat -t mem0-cleanup
    if [ \$? -eq 0 ]; then
        echo '$TODAY' > '$MARKER'
    else
        logger -t mem0-cleanup 'Cleanup failed in background'
    fi
"

exit 0
