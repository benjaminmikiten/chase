"""The live page: is a dog barking right now, and for how long.

Grafana sees barking 15-40 s late (10 s rules tick, 15 s scrape, remote write, panel refresh).
This page is fed by every scored window (every ~0.5 s) and polled once a second, so it's as
current as the detector itself. It only reads in-memory state, so it adds nothing to the tick.

"How long" is `rules.current_run` over the windows of the latest run: the same definition as the
`chase_current_run_seconds` gauge and the episode rules, just computed sooner.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any

from chase.config import RuleConfig
from chase.model import BarkWindow
from chase.rules import current_run

logger = logging.getLogger(__name__)

# Barking "right now" (vs. a pause inside a run): the last bark ended at most this long ago.
# A little over two hops, so the steady stream of overlapping windows never flickers.
ACTIVE_SECONDS = 1.5
# No window scored for this long means the mic or model has stopped; say so instead of "quiet".
STALE_SECONDS = 5.0


def _iso(d: datetime | None) -> str | None:
    return d.isoformat().replace("+00:00", "Z") if d else None


class LiveStatus:
    """Thread-safe: the service loop writes, the HTTP thread reads."""

    def __init__(
        self,
        rules: RuleConfig,
        clock: Callable[[], datetime],
        history: Iterable[BarkWindow] = (),
    ):
        self.rules = rules
        self.clock = clock
        self._lock = threading.Lock()
        self._run: list[BarkWindow] = []  # windows of the latest run only (bounded by its length)
        self._reach: datetime | None = None  # latest end in _run
        self._last_scored: datetime | None = None
        self._last_score: float | None = None
        # Seed from the bark log so a restart mid-bout keeps the bout's true length.
        for w in sorted(history, key=lambda w: (w.start, w.end)):
            self._add(w)

    def scored(self, at: datetime, score: float) -> None:
        """Every scored window, barking or not: liveness and the raw score meter."""
        with self._lock:
            self._last_scored = at
            self._last_score = score

    def bark(self, w: BarkWindow) -> None:
        """A window that was logged as barking."""
        with self._lock:
            self._add(w)

    def _add(self, w: BarkWindow) -> None:
        if (
            self._reach is None
            or (w.start - self._reach).total_seconds() > self.rules.max_gap_seconds
        ):
            self._run = []  # a gap the rules won't bridge: a new run starts here
        self._run.append(w)
        self._reach = max(w.end, self._reach or w.end)

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            run_windows = list(self._run)
            reach, last_scored, last_score = self._reach, self._last_scored, self._last_score

        run = current_run(run_windows, now, self.rules)
        since_bark = max(0.0, (now - reach).total_seconds()) if reach else None
        since_scored = (now - last_scored).total_seconds() if last_scored else None

        if since_scored is None or since_scored > STALE_SECONDS:
            state = "no_audio"
        elif run is not None and since_bark is not None and since_bark <= ACTIVE_SECONDS:
            state = "barking"
        elif run is not None:
            state = "paused"  # quiet, but barking again within max_gap continues the same run
        else:
            state = "quiet"

        return {
            "now": _iso(now),
            "state": state,
            "run": None
            if run is None
            else {
                "start": _iso(run.start),
                "duration_seconds": round(run.duration_seconds, 1),
                "seconds_until_episode": round(run.seconds_until_episode, 1),
                "is_episode": run.seconds_until_episode == 0,
                "resumes_within_seconds": round(
                    max(0.0, self.rules.max_gap_seconds - (since_bark or 0.0)), 1
                ),
            },
            "last_bark_end": _iso(reach),
            "seconds_since_bark": None if since_bark is None else round(since_bark, 1),
            "last_score": None if last_score is None else round(last_score, 3),
            "seconds_since_audio": None if since_scored is None else round(since_scored, 1),
            "rules": {
                "name": self.rules.name,
                "episode_min_minutes": self.rules.episode_min_minutes,
                "max_gap_seconds": self.rules.max_gap_seconds,
                "confidence_threshold": self.rules.confidence_threshold,
            },
        }


def _page() -> bytes:
    return files("chase").joinpath("live.html").read_bytes()


def serve(live: LiveStatus, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Serve the page and its JSON on a daemon thread. Returns the server (for tests/shutdown)."""
    page = _page()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/api/live":
                body = json.dumps(live.snapshot()).encode()
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain", b"not found\n")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # one request a second per open tab; keep the event log clean

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="live-ui", daemon=True).start()
    logger.info("live page on http://%s:%d/", host, server.server_address[1])
    return server
