#!/usr/bin/env bash
set -euo pipefail

/setuid.sh || true

CMD=( python3 /viofosync.py
      "${ADDRESS:-}"
      --destination /recordings
)

# Optional args
[ -n "${PRIORITY:-}"      ] && CMD+=( --priority "${PRIORITY}" )
[ -n "${TIMEOUT:-}"       ] && CMD+=( --timeout "${TIMEOUT}" )
[ -n "${KEEP:-}"          ] && CMD+=( --keep "${KEEP}" )
[ -n "${MAX_USED_DISK:-}" ] && CMD+=( --max-used-disk "${MAX_USED_DISK}" )

# Flags
[ -n "${DRY_RUN:-}"      ] && CMD+=( --dry-run )
[ -n "${GPS_EXTRACT:-}"  ] && CMD+=( --gps-extract )
[ -n "${QUIET:-}"        ] && CMD+=( --quiet )

# Verbosity
if [ "${VERBOSE:-0}" -gt 0 ]; then
  for ((i=0; i<VERBOSE; i++)); do CMD+=( --verbose ); done
fi

# run-once vs monitor
if [ -n "${RUN_ONCE:-}" ]; then
  CMD+=( --run-once )
else
  CMD+=( --monitor )
fi

exec "${CMD[@]}"
