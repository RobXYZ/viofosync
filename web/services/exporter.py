"""ffmpeg export worker.

Jobs live in the ``export_jobs`` SQLite table. A single
:class:`ExportWorker` background task pulls them FIFO, shells
out to ffmpeg, and parses ``-progress pipe:1`` output so the
frontend can show a progress bar via the same WebSocket the
downloader uses.

Job types:
  * ``join_front`` — concat demuxer on front clips only
  * ``join_rear``  — same for rear
  * ``pip``        — picture-in-picture: front fullscreen +
                     rear inset. Requires paired clips.
  * ``pip_rear``   — picture-in-picture with rear fullscreen +
                     front inset. Requires paired clips.

Outputs land in ``$RECORDINGS/.exports/{job_id}.mp4`` and are
served by the archive router via a standard ``FileResponse``.

If ffmpeg isn't installed, jobs are rejected at creation time
with a 503 so the UI can tell the user why.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import List, Optional

from ..db import Database
from ..settings import SettingsProvider
from .naming import channel_of

log = logging.getLogger("viofosync.exporter")

EXPORT_DIR_NAME = ".exports"
PROGRESS_TIME_RE = re.compile(r"out_time_ms=(\d+)")


def exports_dir(recordings: str) -> str:
    d = os.path.join(recordings, EXPORT_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


# Minimum trim length; sub-frame slivers from clamping are dropped.
_MIN_PIECE_S = 0.05


def build_switch_pieces(segments: list, clips: list) -> list:
    """Turn a switched-export plan into an ordered list of trims.

    ``segments`` is ``[{channel, start_ts, end_ts}, ...]`` (in output
    order). ``clips`` is ``[{path, channel, start_ts, duration_s}, ...]``
    (``channel`` already derived via ``naming.channel_of``). Returns
    ``[{path, ss, t}, ...]`` — each piece trims ``path`` from offset
    ``ss`` for duration ``t`` seconds. Clips are clamped to the segment
    window; pieces shorter than ``_MIN_PIECE_S`` are dropped.
    """
    pieces: list = []
    for seg in segments:
        s, e, ch = seg["start_ts"], seg["end_ts"], seg["channel"]
        seg_clips = sorted(
            (
                c for c in clips
                if c["channel"] == ch
                and c["start_ts"] < e
                and c["start_ts"] + (c.get("duration_s") or 0) > s
            ),
            key=lambda c: c["start_ts"],
        )
        for c in seg_clips:
            cs = c["start_ts"]
            ce = cs + (c.get("duration_s") or 0)
            in_ = max(s, cs) - cs
            out_ = min(e, ce) - cs
            if out_ - in_ >= _MIN_PIECE_S:
                pieces.append({
                    "path": c["path"],
                    # float() so integer unix timestamps still yield
                    # float offsets (consistent ffmpeg -ss/-t strings).
                    "ss": round(float(in_), 3),
                    "t": round(float(out_ - in_), 3),
                })
    return pieces


def reconcile_orphan_jobs(db: Database) -> int:
    """Mark rows stuck at ``state='running'`` as failed.

    Intended caller is the lifespan startup hook: if the export
    worker crashed (or the container was replaced) mid-render,
    those rows would otherwise stay "running" forever in the UI.

    Returns the number of rows updated.
    """
    with db.write() as c:
        cur = c.execute(
            "UPDATE export_jobs "
            "SET state='failed', "
            "    error='interrupted by container restart', "
            "    finished_at=? "
            "WHERE state='running'",
            (int(time.time()),),
        )
        return cur.rowcount


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# H.264 encoder options we know about. ``software`` is always
# listed because libx264 ships with every ffmpeg build; the
# others depend on the host's ffmpeg + hardware.
_ENCODER_NEEDLES = {
    "software": "libx264",
    "videotoolbox": "h264_videotoolbox",   # Apple Silicon / Intel Mac
    "nvenc": "h264_nvenc",                 # NVIDIA
    "vaapi": "h264_vaapi",                 # Linux AMD / Intel iGPU
    "qsv": "h264_qsv",                     # Intel Quick Sync
}


def _probe_encoders_sync() -> str:
    """Blocking ffmpeg -encoders call. Kept synchronous on purpose
    so we can run it in a worker thread — asyncio's subprocess
    machinery has been observed to hang on older kernels (e.g.
    Synology DSM 7) where the child becomes a zombie but the
    child-watcher never wakes, blocking startup forever."""
    try:
        result = subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg",
             "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("ffmpeg encoder probe failed: %s", e)
        return ""


def _test_encoder_sync(encoder: str) -> bool:
    """Try to encode a 1-frame synthetic clip with the given
    encoder. Returns True iff ffmpeg exits 0.

    ``ffmpeg -encoders`` only tells us what was compiled in. A
    hardware encoder (qsv/vaapi/nvenc) can be present in the
    build but fail at runtime when its kernel driver, GPU
    device node, or VA-API library isn't reachable from inside
    the container — common on Synology where ``/dev/dri``
    isn't mapped through by default. The 1-frame test exercises
    the exact init path the real export uses, so anything that
    survives this is genuinely usable."""
    if encoder == "software":
        # libx264 ships with every ffmpeg build; the -encoders
        # presence check is enough.
        return True
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", "color=size=64x64:duration=0.1:rate=1",
        *video_codec_args(encoder),
        "-frames:v", "1",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


async def probe_encoders() -> dict:
    """Probe which H.264 encoders this host can actually run.

    Two-pass: first a cheap ``ffmpeg -encoders`` to filter to
    what's compiled in, then a real 1-frame test encode for each
    candidate to weed out hardware encoders whose runtime is
    broken (no GPU passthrough, missing driver, container
    permissions). Result is cached on ``app.state.export_encoders``
    after boot, so the test encodes run exactly once.
    """
    if not ffmpeg_available():
        return {k: False for k in _ENCODER_NEEDLES}
    text = await asyncio.to_thread(_probe_encoders_sync)
    compiled_in = {
        name: (needle in text)
        for name, needle in _ENCODER_NEEDLES.items()
    }

    async def _check(name: str, present: bool) -> tuple[str, bool]:
        if not present:
            return name, False
        works = await asyncio.to_thread(_test_encoder_sync, name)
        if not works and name != "software":
            log.info(
                "encoder %s is compiled in but failed runtime test "
                "— skipping (likely needs GPU passthrough or driver)",
                name,
            )
        return name, works

    pairs = await asyncio.gather(
        *(_check(name, present) for name, present in compiled_in.items())
    )
    return dict(pairs)


# PiP overlay coordinates per corner. ffmpeg's overlay filter
# uses W/H = main video dimensions and w/h = overlay dimensions;
# 20px from the chosen edges keeps the inset clear of safe-area
# crop on most playback paths.
_PIP_OVERLAY_COORDS = {
    "top_right":    "W-w-20:20",
    "top_left":     "20:20",
    "bottom_right": "W-w-20:H-h-20",
    "bottom_left":  "20:H-h-20",
}


def _pip_filter_complex(position: str, main: str = "front") -> str:
    """Build the -filter_complex argument for the PiP overlay.

    ffmpeg input 0 is the front clip, input 1 is the rear clip.
    ``main`` chooses which is the fullscreen base layer; the other
    is scaled to 1/4 size and overlaid. ``main="front"`` (default)
    reproduces the original front-fullscreen behaviour. Unknown
    ``position`` values fall back to ``top_right`` so a typo
    doesn't break ffmpeg invocation entirely.
    """
    coords = _PIP_OVERLAY_COORDS.get(
        position, _PIP_OVERLAY_COORDS["top_right"],
    )
    # Only "front" / "rear" are ever passed (derived from the job
    # type); anything else falls through to rear-main rather than
    # erroring, matching the lenient position handling above.
    base, inset = ("0", "1") if main == "front" else ("1", "0")
    return (
        f"[{inset}:v]scale=iw/4:ih/4[pip];"
        f"[{base}:v][pip]overlay={coords}"
    )


def video_codec_args(encoder: str) -> List[str]:
    """ffmpeg ``-c:v`` arguments for a given encoder. Unknown
    values fall back to software libx264 so a typo can't brick
    an export."""
    if encoder == "videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
    if encoder == "nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23",
        ]
    if encoder == "qsv":
        return [
            "-c:v", "h264_qsv", "-global_quality", "23",
        ]
    # ``software`` (default / fallback) — widely compatible.
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]


class ExportWorker:
    def __init__(
        self,
        db: Database,
        provider: SettingsProvider,
        broadcast,  # callable(dict) -> awaitable — WebSocket hub
    ) -> None:
        self.db = db
        self._provider = provider
        self.broadcast = broadcast
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    # ---- Job creation (called from the HTTP route) ----

    def enqueue(
        self,
        job_type: str,
        clip_ids: List[int],
        encoder: str = "software",
    ) -> int:
        if not ffmpeg_available():
            raise RuntimeError("ffmpeg not installed on this host")
        if job_type not in ("join_front", "join_rear", "pip", "pip_rear"):
            raise ValueError(f"unknown job type: {job_type}")
        if not clip_ids:
            raise ValueError("no clips selected")

        # clip_ids + encoder share one JSON column to avoid a
        # schema migration. The reader accepts both this dict
        # form and legacy rows that stored a bare list.
        payload = json.dumps({
            "clip_ids": clip_ids,
            "encoder": encoder,
        })
        with self.db.write() as c:
            # Snapshot the footage date range now — the source clips
            # may be retention-pruned long before the export is.
            ph = ",".join("?" * len(clip_ids))
            rng = c.execute(
                f"SELECT MIN(timestamp) AS lo, MAX(timestamp) AS hi "
                f"FROM clip_index WHERE id IN ({ph})",
                clip_ids,
            ).fetchone()
            cur = c.execute(
                """
                INSERT INTO export_jobs
                    (type, clip_ids, state, created_at,
                     clip_start, clip_end)
                VALUES (?, ?, 'queued', ?, ?, ?)
                """,
                (job_type, payload, int(time.time()),
                 rng["lo"], rng["hi"]),
            )
            return cur.lastrowid

    def enqueue_switched(self, segments: list, encoder: str = "software") -> int:
        if not ffmpeg_available():
            raise RuntimeError("ffmpeg not installed on this host")
        if not segments:
            raise ValueError("no segments")
        for s in segments:
            if "channel" not in s or "start_ts" not in s or "end_ts" not in s:
                raise ValueError("segment missing channel/start_ts/end_ts")
            if not (s["end_ts"] > s["start_ts"]):
                raise ValueError("segment end_ts must be after start_ts")

        payload = json.dumps({"segments": segments, "encoder": encoder})
        clip_start = int(min(s["start_ts"] for s in segments))
        clip_end = int(max(s["end_ts"] for s in segments))
        with self.db.write() as c:
            cur = c.execute(
                """
                INSERT INTO export_jobs
                    (type, clip_ids, state, created_at, clip_start, clip_end)
                VALUES ('switched', ?, 'queued', ?, ?, ?)
                """,
                (payload, int(time.time()), clip_start, clip_end),
            )
            return cur.lastrowid

    # ---- Background loop ----

    async def _run(self) -> None:
        while not self._stop.is_set():
            job = self._pop_next()
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._run_job(job)
            except Exception as e:  # pragma: no cover
                log.exception("export job %d failed", job["id"])
                self._finish(job["id"], False, str(e), None)

    def _pop_next(self) -> Optional[dict]:
        with self.db.write() as c:
            row = c.execute(
                "SELECT * FROM export_jobs "
                "WHERE state='queued' "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE export_jobs "
                "SET state='running', started_at=? WHERE id=?",
                (int(time.time()), row["id"]),
            )
            return dict(row)

    def _finish(
        self,
        job_id: int,
        ok: bool,
        err: Optional[str],
        output_path: Optional[str],
    ) -> None:
        state = "done" if ok else "failed"
        # progress is REAL NOT NULL — only force it to 1.0 on
        # success. On failure leave it alone so users can see
        # how far the job got. (Earlier we wrote NULL here, which
        # violated the constraint and crashed _finish, leaving
        # the job stuck at state='running' with no broadcast —
        # the UI sat at 0% with no failure indication.)
        with self.db.write() as c:
            if ok:
                c.execute(
                    "UPDATE export_jobs SET state=?, error=?, "
                    "output_path=?, progress=1.0, finished_at=? "
                    "WHERE id=?",
                    (state, err, output_path,
                     int(time.time()), job_id),
                )
            else:
                c.execute(
                    "UPDATE export_jobs SET state=?, error=?, "
                    "output_path=?, finished_at=? WHERE id=?",
                    (state, err, output_path,
                     int(time.time()), job_id),
                )
        asyncio.create_task(
            self.broadcast(
                {
                    "type": "export_finished",
                    "job_id": job_id,
                    "ok": ok,
                    "error": err,
                }
            )
        )

    def _fetch_clips(self, clip_ids: List[int]) -> List[dict]:
        ph = ",".join("?" * len(clip_ids))
        with self.db.conn() as c:
            rows = c.execute(
                f"SELECT id, path, basename, camera, event_type, "
                f"timestamp, sequence FROM clip_index "
                f"WHERE id IN ({ph})",
                clip_ids,
            ).fetchall()
        return sorted(
            (dict(r) for r in rows),
            key=lambda r: (r["timestamp"], r["sequence"]),
        )

    async def _run_job(self, job: dict) -> None:
        snap = self._provider.get()
        raw = json.loads(job["clip_ids"])
        if isinstance(raw, list):
            # Legacy payload shape: bare list of ids.
            clip_ids = raw
            encoder = "software"
        else:
            clip_ids = raw.get("clip_ids", [])
            encoder = raw.get("encoder") or "software"
        out = os.path.join(
            exports_dir(snap.recordings), f"{job['id']}.mp4"
        )

        if job["type"] == "switched":
            segments = raw.get("segments", []) if isinstance(raw, dict) else []
            await self._run_switched(job, segments, encoder, out)
            return

        clips = self._fetch_clips(clip_ids)

        if job["type"] in ("join_front", "join_rear"):
            wanted = "F" if job["type"] == "join_front" else "R"
            # ``camera`` may be ``F``, ``R``, ``PF``, ``PR``, etc.
            # The last letter identifies the lens.
            selected = [
                c for c in clips
                if (c["camera"] or "").upper().endswith(wanted)
            ]
            if not selected:
                self._finish(
                    job["id"], False,
                    f"no {wanted} clips selected", None,
                )
                return
            await self._concat(job["id"], selected, out)
        else:  # pip / pip_rear
            pairs = self._pair_clips(clips)
            if not pairs:
                self._finish(
                    job["id"], False,
                    "no front+rear pairs in selection", None,
                )
                return
            main = "rear" if job["type"] == "pip_rear" else "front"
            await self._pip(
                job["id"], pairs, out, encoder,
                snap.pip_position, main=main,
            )

    # ---- ffmpeg invocations ----

    @staticmethod
    def _pair_clips(clips: List[dict]):
        # Viofo gives F and R from the same capture identical
        # timestamps but consecutive sequences, so key on
        # (timestamp, event_type) and pick the slot from the
        # trailing letter of ``camera`` (handles PF/PR too).
        pairs: dict[tuple[int, str], dict] = {}
        for c in clips:
            cam = (c["camera"] or "").upper()
            kind = c.get("event_type") or "normal"
            key = (c["timestamp"], kind)
            slot = "front" if cam.endswith("F") else "rear"
            pairs.setdefault(key, {})[slot] = c
        return [
            p for p in sorted(pairs.items())
            if "front" in p[1] and "rear" in p[1]
        ]

    async def _concat(
        self, job_id: int, clips: List[dict], out: str
    ) -> None:
        """Use the concat demuxer — fast, no re-encode when
        codecs match (which they do for a single dashcam)."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False
        ) as f:
            list_file = f.name
            for c in clips:
                # Absolute path: ffmpeg's concat demuxer resolves
                # relative entries against the list file's own
                # directory (the temp dir), not our CWD — so a
                # relative clip path (dev boxes with a relative
                # RECORDINGS) would send ffmpeg looking in /tmp.
                # Escape single quotes per the ffmpeg concat docs.
                safe = os.path.abspath(c["path"]).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        try:
            total_duration = await self._probe_total(clips)
            rc, err = await self._run_ffmpeg(
                job_id,
                [
                    "-y", "-f", "concat", "-safe", "0",
                    "-i", list_file, "-c", "copy", out,
                ],
                total_duration,
            )
        finally:
            try:
                os.remove(list_file)
            except OSError:
                pass

        if rc == 0 and os.path.exists(out):
            self._finish(job_id, True, None, out)
        else:
            self._finish(
                job_id, False,
                f"ffmpeg exit {rc}: {err}" if err else f"ffmpeg exit {rc}",
                None,
            )

    async def _pip(
        self, job_id: int, pairs, out: str,
        encoder: str = "software",
        position: str = "top_right",
        main: str = "front",
    ) -> None:
        """One ffmpeg per pair into a temp dir, then concat.

        Broadcasts segment-level progress so the UI shows
        something meaningful even though each segment is a
        separate ffmpeg invocation."""
        tmp = tempfile.mkdtemp(prefix="vfs_pip_")
        parts: List[str] = []
        total_segments = len(pairs)
        filter_complex = _pip_filter_complex(position, main=main)
        try:
            for i, (_, p) in enumerate(pairs):
                # Probe this segment's duration so the inner
                # pump() can emit fine-grained progress instead
                # of sitting silent for minutes. Front and rear of a
                # Viofo pair share a duration, so the front clip is a
                # fine reference even for rear-main (pip_rear).
                seg_dur = await self._probe_total(
                    [p["front"]]
                )
                seg = os.path.join(tmp, f"seg_{i:04d}.mp4")
                # Coarse progress tick so the UI moves when a
                # new segment starts even before ffmpeg output
                # comes through.
                await self.broadcast({
                    "type": "export_progress",
                    "job_id": job_id,
                    "progress": i / max(1, total_segments),
                    "stage": f"segment {i + 1}/{total_segments}",
                })
                rc, err = await self._run_ffmpeg(
                    job_id,
                    [
                        "-y",
                        "-i", p["front"]["path"],
                        "-i", p["rear"]["path"],
                        "-filter_complex",
                        filter_complex,
                        *video_codec_args(encoder),
                        "-c:a", "copy",
                        seg,
                    ],
                    seg_dur,
                    progress_base=i / max(1, total_segments),
                    progress_span=1.0 / max(1, total_segments),
                    stage=f"segment {i + 1}/{total_segments}",
                )
                if rc != 0:
                    self._finish(
                        job_id, False,
                        f"segment {i + 1} failed (ffmpeg exit {rc}): {err}",
                        None,
                    )
                    return
                parts.append(seg)

            await self.broadcast({
                "type": "export_progress",
                "job_id": job_id,
                "progress": 0.98,
                "stage": "concatenating",
            })

            # Final concat of PiP segments.
            list_file = os.path.join(tmp, "parts.txt")
            with open(list_file, "w") as f:
                for p in parts:
                    f.write(f"file '{p}'\n")
            rc, err = await self._run_ffmpeg(
                job_id,
                [
                    "-y", "-f", "concat", "-safe", "0",
                    "-i", list_file, "-c", "copy", out,
                ],
                None,
                stage="concatenating",
            )
            if rc == 0 and os.path.exists(out):
                self._finish(job_id, True, None, out)
            else:
                self._finish(
                    job_id, False,
                    f"concat failed (ffmpeg exit {rc}): {err}", None,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _run_switched(self, job, segments, encoder, out) -> None:
        lo = min(s["start_ts"] for s in segments)
        hi = max(s["end_ts"] for s in segments)
        with self.db.conn() as c:
            rows = c.execute(
                """
                SELECT path, camera, timestamp, duration_s
                FROM clip_index
                WHERE timestamp < ?
                  AND timestamp + COALESCE(duration_s, 0) > ?
                """,
                (hi, lo),
            ).fetchall()
        clips = [
            {
                "path": r["path"],
                "channel": channel_of(r["camera"]),
                "start_ts": r["timestamp"],
                "duration_s": r["duration_s"],
            }
            for r in rows
        ]
        pieces = build_switch_pieces(segments, clips)
        if not pieces:
            self._finish(job["id"], False, "no footage in selection", None)
            return

        res = await self._probe_resolution(pieces[0]["path"])
        w, h = res if res else (1920, 1080)
        vf = f"scale={w}:{h},setsar=1"

        tmp = tempfile.mkdtemp(prefix="vfs_switched_")
        parts: List[str] = []
        n = len(pieces)
        try:
            for i, pc in enumerate(pieces):
                seg = os.path.join(tmp, f"seg_{i:04d}.mp4")
                await self.broadcast({
                    "type": "export_progress", "job_id": job["id"],
                    "progress": i / max(1, n),
                    "stage": f"segment {i + 1}/{n}",
                })
                rc, err = await self._run_ffmpeg(
                    job["id"],
                    [
                        "-y",
                        "-ss", str(pc["ss"]),
                        "-i", pc["path"],
                        "-t", str(pc["t"]),
                        "-vf", vf,
                        *video_codec_args(encoder),
                        "-c:a", "aac",
                        seg,
                    ],
                    pc["t"],
                    progress_base=i / max(1, n),
                    progress_span=1.0 / max(1, n),
                    stage=f"segment {i + 1}/{n}",
                )
                if rc != 0:
                    self._finish(
                        job["id"], False,
                        f"segment {i + 1} failed (ffmpeg exit {rc}): {err}",
                        None,
                    )
                    return
                parts.append(seg)

            await self.broadcast({
                "type": "export_progress", "job_id": job["id"],
                "progress": 0.98, "stage": "concatenating",
            })
            list_file = os.path.join(tmp, "parts.txt")
            with open(list_file, "w") as f:
                for p in parts:
                    safe = os.path.abspath(p).replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")
            rc, err = await self._run_ffmpeg(
                job["id"],
                ["-y", "-f", "concat", "-safe", "0",
                 "-i", list_file, "-c", "copy", out],
                None, stage="concatenating",
            )
            if rc == 0 and os.path.exists(out):
                self._finish(job["id"], True, None, out)
            else:
                self._finish(
                    job["id"], False,
                    f"concat failed (ffmpeg exit {rc}): {err}", None,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _probe_total(self, clips: List[dict]) -> Optional[float]:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return None
        total = 0.0
        for c in clips:
            proc = await asyncio.create_subprocess_exec(
                ffprobe,
                "-v", "error", "-show_entries",
                "format=duration", "-of",
                "default=noprint_wrappers=1:nokey=1",
                c["path"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            try:
                total += float(out.decode().strip())
            except ValueError:
                return None
        return total

    async def _probe_resolution(self, path: str):
        """(width, height) of the first video stream, or None."""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return None
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        try:
            w, h = out.decode().strip().split("x")
            return int(w), int(h)
        except ValueError:
            return None

    async def _run_ffmpeg(
        self,
        job_id: int,
        args: List[str],
        total_duration: Optional[float],
        *,
        progress_base: float = 0.0,
        progress_span: float = 1.0,
        stage: Optional[str] = None,
    ) -> tuple[int, str]:
        """Run ffmpeg and return (returncode, last-stderr-lines).

        stderr is drained concurrently — without this, a verbose
        ffmpeg would block once the OS pipe buffer filled up,
        hanging the job. We also return the tail of stderr so
        the UI can surface the real error instead of a bare
        ``ffmpeg exit 1``."""
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats",
            *args,
        ]
        log.info("export job %d: %s", job_id, " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stderr_tail: list[str] = []

        async def pump_stdout():
            assert proc.stdout is not None
            last = 0.0
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="ignore")
                m = PROGRESS_TIME_RE.search(text)
                if m and total_duration:
                    done_s = int(m.group(1)) / 1_000_000
                    inner = min(1.0, done_s / total_duration)
                    frac = progress_base + inner * progress_span
                    now = time.monotonic()
                    if now - last > 0.25:
                        last = now
                        ev = {
                            "type": "export_progress",
                            "job_id": job_id,
                            "progress": frac,
                        }
                        if stage:
                            ev["stage"] = stage
                        await self.broadcast(ev)

        async def pump_stderr():
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").rstrip()
                if text:
                    log.warning("ffmpeg[%d]: %s", job_id, text)
                    stderr_tail.append(text)
                    # Cap memory — we only need the last few
                    # lines for the error message.
                    if len(stderr_tail) > 20:
                        del stderr_tail[0]

        await asyncio.gather(
            pump_stdout(), pump_stderr(), proc.wait()
        )
        rc = proc.returncode or 0
        err = " | ".join(stderr_tail[-3:]) if stderr_tail else ""
        return rc, err
