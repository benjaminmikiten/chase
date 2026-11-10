"""The report service: Pub/Sub push in, one ready-to-forward email out (ADR 0005).

POST / receives a GCS OBJECT_FINALIZE notification for violations/<vid>/rev-<n>.json:
- 204: done, nothing to do, or a duplicate;
- 500: failed, so Pub/Sub retries (and eventually dead-letters it);
- 400: not a Pub/Sub push at all.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from flask import Flask, request

from chase_report.assemble import assemble, ffmpeg_mp3
from chase_report.mail import Sender, attach_audio, compose
from chase_report.model import Manifest
from chase_report.render import build_view, render_html, render_pdf
from chase_report.store import Store
from chase_report.trigger import decide, manifest_key, parse_push, release

logger = logging.getLogger(__name__)


class Mailer(Protocol):
    def send(self, msg: Any) -> None: ...


def log(event: str, severity: str = "INFO", **fields: Any) -> None:
    """One JSON line per event; Cloud Logging reads `severity` and indexes the rest."""
    sys.stdout.write(json.dumps({"severity": severity, "event": event, **fields}, default=str))
    sys.stdout.write("\n")
    sys.stdout.flush()


def process(
    store: Store,
    name: str,
    sender: Sender,
    mailer: Mailer,
    workdir: Path,
    today: date | None = None,
    limit_mb: float = 15.0,
    encode: Callable[[Path, Path], None] = ffmpeg_mp3,
    pdf_renderer: Callable[[str, Path], None] = render_pdf,
) -> str:
    decision = decide(store, name)
    if decision != "process":
        return {"ignore": "ignored"}.get(decision, decision)

    key = manifest_key(name)
    assert key is not None
    vid, rev = key
    base = f"evidence/{vid}/rev-{rev}/"
    sent = False
    try:
        manifest = Manifest.from_json(json.loads(store.read_bytes(name)))
        evidence = assemble(store, manifest, workdir, encode)
        tzname = (
            evidence.episodes[0].meta.timezone if evidence.episodes else None
        ) or sender.timezone
        tz = ZoneInfo(tzname)
        today = today or datetime.now(tz).date()

        links: dict[str, str] = {
            f"Episode {i} recording (FLAC original)": store.signed_url(e.flac_name, hours=168)
            for i, e in enumerate(evidence.episodes, 1)
        }
        view = build_view(evidence, sender, links, today)
        pdf = workdir / f"barking-report-{view.day.isoformat()}-r{rev}.pdf"
        pdf_renderer(render_html(view), pdf)
        store.upload(base + "report.pdf", pdf, "application/pdf")

        mp3s: list[tuple[str, Path]] = []
        if attach_audio(pdf.stat().st_size, [e.mp3_bytes for e in evidence.episodes], limit_mb):
            for i, e in enumerate(evidence.episodes, 1):
                path = workdir / f"episode-{i}-{e.meta.start.astimezone(tz):%Y-%m-%d-%H%M}.mp3"
                store.download(e.mp3_name, path)
                mp3s.append((path.name, path))
        else:
            for i, e in enumerate(evidence.episodes, 1):
                links[f"Episode {i} recording (MP3 copy, {e.mp3_name.rsplit('/', 1)[-1]})"] = (
                    store.signed_url(e.mp3_name, hours=168)
                )

        sent_rev = re.compile(rf"^evidence/{re.escape(vid)}/rev-(\d+)/sent\.json$")
        updated = any(
            (m := sent_rev.match(n)) and int(m.group(1)) < rev
            for n in store.list(f"evidence/{vid}/")
        )
        msg = compose(view, pdf, mp3s, links, sender, updated)
        mailer.send(msg)
        sent = True
        store.put_bytes(
            base + "sent.json",
            json.dumps(
                {
                    "sent_at": datetime.now(UTC).isoformat(),
                    "message_id": msg["Message-ID"],
                    "to": sender.to_email,
                    "attachments": [p.get_filename() for p in msg.iter_attachments()],
                }
            ).encode(),
            "application/json",
        )
        log("report_sent", violation_id=vid, revision=rev, attachments=len(mp3s) + 1)
        return "sent"
    except Exception as e:
        if sent:
            # The email went out; keep the claim so a retry can never send it twice.
            log("report_failed_after_send", "ERROR", violation_id=vid, revision=rev, error=str(e))
        else:
            release(store, vid, rev)
        raise
    finally:
        for p in workdir.iterdir():
            if p.is_file():
                p.unlink(missing_ok=True)


def create_app(
    store_factory: Callable[[str], Store],
    sender_loader: Callable[[], Sender],
    mailer_factory: Callable[[], Mailer],
    workdir: Path | None = None,
    encode: Callable[[Path, Path], None] = ffmpeg_mp3,
    pdf_renderer: Callable[[str, Path], None] = render_pdf,
    limit_mb: float | None = None,
) -> Flask:
    app = Flask(__name__)

    @app.post("/")
    def push():  # type: ignore[no-untyped-def]
        envelope = request.get_json(silent=True)
        if not isinstance(envelope, dict) or "message" not in envelope:
            log("report_bad_request", "WARNING")
            return "not a Pub/Sub push", 400
        target = parse_push(envelope)
        if target is None:
            log("report_ignored", reason="not an OBJECT_FINALIZE notification")
            return "", 204
        bucket, name = target
        limit = (
            limit_mb if limit_mb is not None else float(os.environ.get("ATTACHMENT_LIMIT_MB", 15))
        )
        tmp = workdir or Path(tempfile.mkdtemp(prefix="chase-report-"))
        try:
            outcome = process(
                store_factory(bucket),
                name,
                sender_loader(),
                mailer_factory(),
                tmp,
                limit_mb=limit,
                encode=encode,
                pdf_renderer=pdf_renderer,
            )
        except Exception as e:
            logger.exception("report failed")
            log("report_failed", "ERROR", object=name, error=f"{type(e).__name__}: {e}")
            return "failed", 500
        finally:
            if workdir is None:
                shutil.rmtree(tmp, ignore_errors=True)
        if outcome != "sent":
            log(f"report_{outcome}", object=name)
        return "", 204

    @app.get("/healthz")
    def healthz():  # type: ignore[no-untyped-def]
        return "ok", 200

    return app


def create_from_env() -> Flask:  # pragma: no cover - production wiring (gunicorn factory)
    from chase_report.mail import SmtpSender
    from chase_report.store import GcsStore

    def sender() -> Sender:
        return Sender.from_json(os.environ["REPORT_CONFIG"])

    def mailer() -> Mailer:
        s = sender()
        return SmtpSender(s.email, os.environ["GMAIL_APP_PASSWORD"])

    return create_app(store_factory=GcsStore, sender_loader=sender, mailer_factory=mailer)
