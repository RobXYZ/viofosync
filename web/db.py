"""SQLite state store for the web UI.

Lives at ``$CONFIG_DIR/viofosync.db`` (default
``/config/viofosync.db``) so per-write syscalls hit a fast local
volume rather than the NAS-backed recordings mount. On first boot
under this code path a DB at the legacy
``$RECORDINGS/.viofosync.db`` location is copied over and the
legacy file is renamed to ``.viofosync.db.migrated`` so an
operator has a fallback. See ``migrate_legacy_db_path`` for the
one-shot migration helper; ``Database.__init__`` itself does not
run migration so tests can construct ``Database(tmp_path)`` safely.

WAL mode so readers don't block the SyncWorker writer.

Schema is created on first boot and migrated idempotently via
``CREATE TABLE IF NOT EXISTS``. There is no schema migration
system yet — keep columns additive and nullable.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .services.naming import (
    camera_letter_sql,
    event_type_sql,
    is_registry_camera_sql,
)

from . import fsinfo

log = logging.getLogger("viofosync.db")


def warn_if_network_volume(db_path: str) -> Optional[str]:
    """Warn when the state DB sits on a network filesystem.

    WAL mode needs shared memory (the mmap'd ``-shm`` file) and
    dependable POSIX locking; NFS/CIFS provide neither, so the failure
    modes are "database is locked", "disk I/O error" and corruption —
    silent and destructive, unlike a refused download. Nothing stops a
    user bind-mounting CONFIG_DIR onto the NAS alongside RECORDINGS,
    so say it out loud at startup rather than in a docstring.

    Returns the offending fstype when it warned, else None (including
    when the mount type can't be determined — no false alarms).
    """
    fstype = fsinfo.fstype_of(
        os.path.abspath(os.path.dirname(db_path) or ".")
    )
    if not fsinfo.is_network_fstype(fstype):
        return None
    log.warning(
        "the state database is on a %s mount (%s): SQLite's WAL mode is "
        "not safe on network filesystems and can report 'database is "
        "locked' or corrupt the file. Point CONFIG_DIR at local storage "
        "— only RECORDINGS belongs on the NAS.",
        fstype, db_path,
    )
    return fstype


def default_db_path() -> str:
    """Return the production location of the state DB.

    Lives under CONFIG_DIR (default /config) so per-write syscalls
    hit a fast local volume rather than the NAS-backed recordings
    mount. The leading dot is dropped from the filename — there is
    no longer any reason to hide the DB on its own dedicated volume.
    """
    return str(
        Path(os.environ.get("CONFIG_DIR", "/config")) / "viofosync.db"
    )


def migrate_legacy_db_path(new_path: str) -> None:
    """Copy a legacy ${RECORDINGS}/.viofosync.db to ``new_path`` on
    first boot. Idempotent: no-op when the new path already exists
    or when the legacy path is missing.

    Call exactly once at startup, before constructing ``Database``.
    Not invoked from ``Database.__init__`` so test code constructing
    ``Database(tmp_path)`` never reads the host's RECORDINGS env var.
    """
    if os.path.exists(new_path):
        return
    legacy_recordings = os.environ.get("RECORDINGS", "/recordings")
    legacy_path = os.path.join(legacy_recordings, ".viofosync.db")
    if not os.path.exists(legacy_path):
        return

    os.makedirs(os.path.dirname(new_path) or ".", exist_ok=True)
    log.info("migrating state db: %s -> %s", legacy_path, new_path)

    # Main file via temp suffix + os.replace so a crash mid-copy
    # leaves no half-written file at the destination.
    tmp = new_path + ".part"
    shutil.copy2(legacy_path, tmp)
    os.replace(tmp, new_path)

    # Sidecars best-effort. SQLite reconstructs from the main file
    # alone if these are missing or stale, so a failure here is not
    # fatal — log and continue.
    for suffix in ("-wal", "-shm"):
        src = legacy_path + suffix
        if not os.path.exists(src):
            continue
        try:
            shutil.copy2(src, new_path + suffix)
        except OSError as e:
            # best-effort: SQLite reconstructs from the main file alone
            log.warning("could not copy %s%s: %s", legacy_path, suffix, e)

    # Rename the legacy file so we don't re-migrate next boot, and
    # so the operator has a recoverable copy. Legacy -wal / -shm
    # files are left in place — harmless.
    try:
        os.replace(legacy_path, legacy_path + ".migrated")
    except OSError as e:  # pragma: no cover — non-fatal
        log.warning("could not rename legacy db: %s", e)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_index (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,
    basename      TEXT NOT NULL,
    group_name    TEXT,
    timestamp     INTEGER NOT NULL,   -- unix seconds
    camera        TEXT NOT NULL,      -- registry letter (F/R/T/I), possibly P/E-prefixed
    sequence      INTEGER NOT NULL,
    event_type    TEXT,               -- 'normal'|'parking'|'ro'
    size_bytes    INTEGER,
    has_gpx       INTEGER NOT NULL DEFAULT 0,
    gps_examined  INTEGER NOT NULL DEFAULT 0,
    locked        INTEGER NOT NULL DEFAULT 0,  -- user "retain indefinitely"
    duration_s    REAL,
    scanned_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clip_index_ts
    ON clip_index(timestamp);
CREATE INDEX IF NOT EXISTS idx_clip_index_group
    ON clip_index(group_name);
-- basename lookups: the archive's GPS-sibling probe (prefix range) and the
-- delete/lock paths all address clips by basename.
CREATE INDEX IF NOT EXISTS idx_clip_index_basename
    ON clip_index(basename);

CREATE TABLE IF NOT EXISTS download_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL UNIQUE,
    source_dir      TEXT NOT NULL,
    remote_size     INTEGER,
    recorded_at     INTEGER,
    camera          TEXT,
    event_type      TEXT,
    state           TEXT NOT NULL,    -- pending|downloading|done|failed|gone|skipped
    priority        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_at INTEGER,
    enqueued_at     INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER,
    manual          INTEGER NOT NULL DEFAULT 0,
    skip_reason     TEXT,             -- 'geofence' (auto) | 'user' | NULL
    geofence_released_at INTEGER,     -- set when a geofence-skip is manually released
    locked          INTEGER NOT NULL DEFAULT 0, -- user "retain indefinitely"
    triage_attempts INTEGER NOT NULL DEFAULT 0,  -- failed-triage retry counter
    triage_last_attempt_at INTEGER,              -- last triage attempt (backoff)
    remote_complete INTEGER                      -- 1 = finalized moov seen; NULL = unconfirmed
);
CREATE INDEX IF NOT EXISTS idx_queue_state
    ON download_queue(state, priority DESC, enqueued_at ASC);

CREATE TABLE IF NOT EXISTS export_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,      -- join_/pip_ per camera (see naming.EXPORT_JOB_TYPES) or timeline
    clip_ids      TEXT NOT NULL,      -- JSON array
    state         TEXT NOT NULL,      -- queued|running|done|failed|cancelled
    progress      REAL NOT NULL DEFAULT 0.0,
    output_path   TEXT,
    error         TEXT,
    created_at    INTEGER NOT NULL,
    started_at    INTEGER,
    finished_at   INTEGER,
    clip_start    INTEGER,            -- min source-clip timestamp (unix s)
    clip_end      INTEGER             -- max source-clip timestamp (unix s)
);

CREATE TABLE IF NOT EXISTS derive_queue (
    clip_id     INTEGER PRIMARY KEY,   -- clip_index.id
    priority    INTEGER NOT NULL,      -- 0 = live (new clip), 1 = backfill
    state       TEXT NOT NULL,         -- pending|running|done|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    enqueued_at INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_derive_state
    ON derive_queue(state, priority ASC, clip_id DESC);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    lat_key     REAL NOT NULL,
    lon_key     REAL NOT NULL,
    label       TEXT NOT NULL,
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (lat_key, lon_key)
);

CREATE TABLE IF NOT EXISTS app_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,   -- record.created (unix seconds, fractional)
    levelno   INTEGER NOT NULL,   -- 10/20/30/40/50; "WARNING+" = levelno >= 30
    level     TEXT    NOT NULL,   -- 'INFO','WARNING','ERROR',...
    logger    TEXT    NOT NULL,   -- record.name, e.g. 'viofosync.sync_worker'
    message   TEXT    NOT NULL,   -- record.getMessage()
    exc_text  TEXT                -- formatted traceback, NULL when none
);
CREATE INDEX IF NOT EXISTS idx_app_log_levelno ON app_log(levelno, id DESC);
"""


class Database:
    """Thread-safe SQLite wrapper.

    SQLite's own thread-safety guarantees work for us because
    we're using ``check_same_thread=False`` with a short-lived
    connection per operation via the :meth:`conn` context
    manager. A single shared connection would be simpler but
    risks contention with the async workers.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # One initial connection just to set pragmas + schema.
        with self.conn() as c:
            c.executescript(_SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Idempotent column-additions for databases that pre-date
        the current schema. ``CREATE TABLE IF NOT EXISTS`` only
        creates fresh tables, so any column added after the first
        release needs an explicit ALTER on existing volumes.

        SQLite raises ``OperationalError("duplicate column …")``
        when the column is already present, which we treat as a
        success. Any other error propagates so an actually-broken
        DB doesn't get silently ignored.
        """
        def _add_column(table: str, col: str, ddl: str) -> None:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

        _add_column(
            "clip_index", "gps_examined",
            "INTEGER NOT NULL DEFAULT 0",
        )
        # Clips that already have a sidecar have, by definition,
        # been examined — backfill the flag so the manual
        # "Extract GPS" button doesn't waste cycles re-parsing
        # them. Idempotent: rows already at 1 stay at 1.
        c.execute(
            "UPDATE clip_index SET gps_examined = 1 "
            "WHERE has_gpx = 1 AND gps_examined = 0"
        )

        # Date range of an export's source clips, snapshotted at
        # enqueue time so the export list can show it after the
        # underlying clips are retention-pruned.
        _add_column("export_jobs", "clip_start", "INTEGER")
        _add_column("export_jobs", "clip_end", "INTEGER")

        # Finished-output stats, snapshotted at finish so the export list can
        # show length + size without re-probing the file on every poll (and
        # even after the output is later removed).
        _add_column("export_jobs", "output_size", "INTEGER")
        _add_column("export_jobs", "output_duration_s", "REAL")

        # GPS Triage: skeleton-track state for queued, not-yet-downloaded
        # clips. triaged_at NULL = not triaged; gps_points NULL = not
        # triaged, 0 = triaged but no fix (never retried), >0 = points found.
        _add_column("download_queue", "triaged_at", "INTEGER")
        _add_column("download_queue", "gps_points", "INTEGER")

        # Triage retry bookkeeping (resilient-gps-triage): attempt counter +
        # last-attempt timestamp so failed triage reads back off and give up
        # after a bounded number of *unreadable* attempts. triage_attempts is
        # bumped only for clip-specific failures, never for camera-offline.
        _add_column(
            "download_queue", "triage_attempts",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column("download_queue", "triage_last_attempt_at", "INTEGER")

        # Geofence exclusion: skip provenance + permanent-release marker.
        # skip_reason 'geofence' = auto-skipped while parked at home,
        # 'user' = manually skipped, NULL = legacy / not skipped.
        # geofence_released_at: when a user un-skips a geofence clip, so the
        # geofence never re-skips it.
        _add_column("download_queue", "skip_reason", "TEXT")
        _add_column("download_queue", "geofence_released_at", "INTEGER")

        # User "retain indefinitely" flag. Distinct from event_type='ro'
        # (dashcam-locked): a user pin that retention always honours.
        _add_column("clip_index", "locked", "INTEGER NOT NULL DEFAULT 0")
        _add_column("download_queue", "locked", "INTEGER NOT NULL DEFAULT 0")

        # Active-recording guard: 1 once a finalized moov is confirmed on the
        # camera; NULL = unconfirmed (newest capture may still be recording).
        # One-way — finalization is irreversible, so it never needs clearing.
        _add_column("download_queue", "remote_complete", "INTEGER")

        # Rows queued while their filename layout went unrecognised kept a
        # NULL camera/event_type (the enqueue-time parser only knew the
        # layouts with a YYYY/MMDD separator). remote_day_clips reads both
        # straight off the row, so those clips mis-pair and dodge the
        # parking filter until the card rotates them out. Backfill from the
        # filename, which carries both. Idempotent, and strictly a
        # fill-the-blanks: COALESCE leaves any value already there alone
        # (event_type carries 'ro' from provenance the filename can't
        # express), as are names that still don't parse.
        c.execute(
            f"UPDATE download_queue "
            f"   SET camera = COALESCE(camera, "
            f"                         {camera_letter_sql('filename')}), "
            f"       event_type = COALESCE(event_type, "
            f"                             {event_type_sql('filename')}) "
            f" WHERE (camera IS NULL OR event_type IS NULL) "
            f"   AND {is_registry_camera_sql('filename')}"
        )

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection with row-factory set.

        We serialise writers via ``self._lock`` because WAL
        allows concurrent readers but only one writer.
        """
        c = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,   # autocommit
            check_same_thread=False,
        )
        c.row_factory = sqlite3.Row
        # Retry on SQLITE_BUSY for 30 s before surfacing
        # OperationalError — lets a brief writer collision during
        # the initial scan resolve silently.
        c.execute("PRAGMA busy_timeout=30000")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialised write connection."""
        with self._lock, self.conn() as c:
            yield c

    # ---- kv: small persisted app state (string-valued) ----

    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.conn() as c:
            row = c.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self.write() as c:
            c.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
