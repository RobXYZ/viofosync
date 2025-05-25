#!/usr/bin/env bash
/setuid.sh

# Build the common base command
CMD=( python3 /viofosync.py
      "$ADDRESS"
      --destination /recordings \
#      --grouping   "$GROUPING"
      --priority   "$PRIORITY"
      --timeout    "$TIMEOUT"
)

# Map booleans
[ -n "$DRY_RUN"    ] && CMD+=( --dry-run )
[ -n "$GPS_EXTRACT" ] && CMD+=( --gps-extract )
[ -n "$QUIET"      ] && CMD+=( --quiet )

# Verbosity
[ "$VERBOSE" -gt 0 ] && for i in $(seq 1 $VERBOSE); do CMD+=( --verbose ); done

# Decide one-shot vs. monitor
if [ -n "$RUN_ONCE" ]; then
  CMD+=( --run-once )
else
  CMD+=( --monitor )
fi

exec "${CMD[@]}"
