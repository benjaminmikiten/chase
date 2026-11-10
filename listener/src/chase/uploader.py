"""Ship the evidence outbox to GCS (Plan 2), with a create-only service-account key.

- Episodes go up first (audio, then metadata). A violation manifest is uploaded only once every
  episode it lists is up, because its arrival is what triggers the report.
- Objects are created with if_generation_match=0. "Already exists" (412) counts as done, and
  nothing ever needs read, list or delete permission, which the key doesn't have.
- Uploaded entries move to data/sent/ (kept `uploaded_days`), so the outbox only holds what's
  pending.
- Failures are counted (`chase_audio_errors_total{reason="upload"}`) and retried next round.
  They never stop the listener.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

from chase.config import Settings
from chase.metrics import Metrics

logger = logging.getLogger(__name__)


class UploadError(Exception):
    pass


class BucketWriter(Protocol):
    def create(self, name: str, path: Path, content_type: str) -> bool:
        """Create the object; False if it already existed; UploadError on anything else."""
        ...


class GcsBucketWriter:  # pragma: no cover - real GCS; verified by infra/smoke-iam.sh
    def __init__(self, bucket: str, credentials: Path):
        from google.cloud import storage

        self._bucket = storage.Client.from_service_account_json(str(credentials)).bucket(bucket)

    def create(self, name: str, path: Path, content_type: str) -> bool:
        from google.api_core.exceptions import PreconditionFailed

        try:
            self._bucket.blob(name).upload_from_filename(
                str(path), content_type=content_type, if_generation_match=0
            )
        except PreconditionFailed:
            return False
        except Exception as e:  # network, auth, permission: retry next round
            raise UploadError(f"{name}: {type(e).__name__}: {e}") from e
        return True


class Uploader:
    def __init__(self, outbox: Path, sent_dir: Path, writer: BucketWriter, metrics: Metrics):
        self.outbox = outbox
        self.sent = sent_dir
        self.writer = writer
        self.metrics = metrics

    def _fail(self, what: str, e: Exception) -> None:
        logger.warning("upload failed (%s): %s", what, e)
        self.metrics.audio_errors.labels(reason="upload").inc()

    def _move(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        shutil.move(str(src), str(dst))

    def _upload_episodes(self) -> int:
        done = 0
        root = self.outbox / "episodes"
        for d in sorted(root.iterdir()) if root.exists() else []:
            if not d.is_dir() or d.name.endswith(".tmp"):
                continue
            try:
                for fname, ctype in (
                    ("audio.flac", "audio/flac"),
                    ("metadata.json", "application/json"),
                ):
                    self.writer.create(f"episodes/{d.name}/{fname}", d / fname, ctype)
            except (UploadError, OSError) as e:
                self._fail(d.name, e)
                continue
            self._move(d, self.sent / "episodes" / d.name)
            done += 1
        return done

    def _upload_manifests(self) -> int:
        done = 0
        root = self.outbox / "violations"
        for path in sorted(root.glob("*/rev-*.json")) if root.exists() else []:
            try:
                ids = json.loads(path.read_text()).get("episode_ids", [])
            except (OSError, ValueError) as e:
                self._fail(str(path), e)
                continue
            if not all((self.sent / "episodes" / eid).is_dir() for eid in ids):
                continue  # its episodes aren't all up yet; the report would fail without them
            vid = path.parent.name
            try:
                self.writer.create(f"violations/{vid}/{path.name}", path, "application/json")
            except (UploadError, OSError) as e:
                self._fail(f"{vid}/{path.name}", e)
                continue
            self._move(path, self.sent / "violations" / vid / path.name)
            done += 1
        return done

    def run_once(self) -> int:
        return self._upload_episodes() + self._upload_manifests()


def prune_sent(sent_dir: Path, now: datetime, days: int) -> int:
    """Delete uploaded entries older than `days` (by directory/file mtime)."""
    cutoff = now.timestamp() - days * 86400
    removed = 0
    for entry in [*sent_dir.glob("episodes/*"), *sent_dir.glob("violations/*/rev-*.json")]:
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                removed += 1
        except OSError:
            logger.warning("could not prune %s", entry)
    return removed


def build_uploader(settings: Settings, metrics: Metrics) -> Uploader | None:
    """The configured uploader, or None (logged; never raises) when upload can't run."""
    if not settings.gcs_bucket:
        logger.info("cloud upload disabled (gcs_bucket not set); evidence stays in the outbox")
        return None
    key = settings.gcs_credentials
    try:
        if not key.is_file():
            raise FileNotFoundError(f"service-account key not found at {key}")
        writer = GcsBucketWriter(settings.gcs_bucket, key)
    except Exception as e:
        logger.warning("cloud upload disabled: %s", e)
        metrics.audio_errors.labels(reason="upload").inc()
        return None
    logger.info("uploading evidence to gs://%s", settings.gcs_bucket)
    return Uploader(settings.data_dir / "outbox", settings.data_dir / "sent", writer, metrics)


def run_forever(uploader: Uploader, settings: Settings, stop, clock=time.time) -> None:
    """Upload loop for a daemon thread: every upload_interval_seconds until `stop` is set."""
    from datetime import UTC

    last_prune = 0.0
    while not stop.wait(settings.upload_interval_seconds):
        try:
            uploader.run_once()
            if clock() - last_prune > 3600:
                prune_sent(uploader.sent, datetime.now(UTC), settings.uploaded_days)
                last_prune = clock()
        except Exception:
            logger.exception("upload round failed")
            uploader.metrics.audio_errors.labels(reason="upload").inc()
