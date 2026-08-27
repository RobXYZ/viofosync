"""Shared pytest fixtures for the viofosync test suite."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Pin the process TZ so the suite runs identically on any dev
# machine. Fixtures seed clip epochs as UTC parses of the same
# wall-clock their GPX sidecars carry; since the GPX clock
# detection (services/gps._rebase_to_filename_clock) anchors
# journey epochs to the container-local filename parse, a non-UTC
# host TZ would split the two by its offset. Tests that exercise
# TZ behaviour itself override this locally and restore it
# (tests/test_gpx_clock_detection.py).
os.environ["TZ"] = "UTC"
time.tzset()


@pytest.fixture
def tmp_config_dir(monkeypatch) -> Iterator[Path]:
    """Isolated /config directory for the duration of one test.

    Sets the CONFIG_DIR env var so code under test reads/writes
    inside the tempdir instead of /config on the host.
    """
    d = Path(tempfile.mkdtemp(prefix="viofosync-cfg-"))
    monkeypatch.setenv("CONFIG_DIR", str(d))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmp_recordings_dir(monkeypatch) -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="viofosync-rec-"))
    monkeypatch.setenv("RECORDINGS", str(d))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session")
def mqtt_broker():
    """Start an in-process amqtt broker on a random port for the session."""
    import asyncio
    import socket
    import threading
    import warnings

    # Pick a free port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    cfg = {
        "listeners": {
            "default": {
                "type": "tcp",
                "bind": f"127.0.0.1:{port}",
                "max_connections": 50,
            },
        },
        "sys_interval": 0,
        "auth": {"allow-anonymous": True},
        "topic-check": {"enabled": False},
    }

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    broker_holder: list = []

    def _runner():
        asyncio.set_event_loop(loop)
        # Suppress amqtt deprecation warnings about old config keys.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from amqtt.broker import Broker
            broker = Broker(cfg, loop=loop)
        broker_holder.append(broker)
        loop.run_until_complete(broker.start())
        ready.set()
        loop.run_forever()
        loop.close()

    t = threading.Thread(target=_runner, daemon=True, name="amqtt-broker")
    t.start()
    ready.wait(timeout=5.0)
    try:
        yield ("127.0.0.1", port)
    finally:
        broker = broker_holder[0]

        async def _shutdown():
            await broker.shutdown()

        asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=5.0)
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5.0)
