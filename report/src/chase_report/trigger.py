"""Which deliveries to act on.

Pub/Sub push delivers at least once and may redeliver while a send is in progress. Exactly one
email per manifest revision is guaranteed by claiming the revision with a create-if-absent marker
BEFORE doing anything else (ADR 0005).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from chase_report.store import Store

logger = logging.getLogger(__name__)

MANIFEST_RE = re.compile(r"^violations/(v-\d{8}T\d{6}Z)/rev-(\d+)\.json$")

Decision = Literal["ignore", "stale", "duplicate", "process"]


def parse_push(envelope: Any) -> tuple[str, str] | None:
    """(bucket, object) from a Pub/Sub push of a GCS OBJECT_FINALIZE notification."""
    try:
        attrs = envelope["message"]["attributes"]
    except (KeyError, TypeError):
        return None
    if attrs.get("eventType") != "OBJECT_FINALIZE":
        return None
    bucket, obj = attrs.get("bucketId"), attrs.get("objectId")
    if not bucket or not obj:
        return None
    return bucket, obj


def manifest_key(name: str) -> tuple[str, int] | None:
    m = MANIFEST_RE.match(name)
    return (m.group(1), int(m.group(2))) if m else None


def latest_revision(store: Store, violation_id: str) -> int:
    revs = [manifest_key(n) for n in store.list(f"violations/{violation_id}/")]
    return max((k[1] for k in revs if k), default=0)


def claim_name(violation_id: str, revision: int) -> str:
    return f"evidence/{violation_id}/rev-{revision}/claimed.json"


def claim(store: Store, violation_id: str, revision: int) -> bool:
    body = json.dumps({"claimed_at": datetime.now(UTC).isoformat()}).encode()
    return store.put_bytes(
        claim_name(violation_id, revision), body, "application/json", if_absent=True
    )


def release(store: Store, violation_id: str, revision: int) -> None:
    store.delete(claim_name(violation_id, revision))


STALE_CLAIM = timedelta(minutes=15)  # well past the 600 s request timeout


def _claimed_at(store: Store, violation_id: str, revision: int) -> datetime | None:
    try:
        raw = json.loads(store.read_bytes(claim_name(violation_id, revision)))
        return datetime.fromisoformat(raw["claimed_at"])
    except (FileNotFoundError, KeyError, ValueError, TypeError):
        return None


def decide(store: Store, name: str, now: datetime | None = None) -> Decision:
    key = manifest_key(name)
    if key is None:
        return "ignore"
    vid, rev = key
    if rev < latest_revision(store, vid):
        return "stale"  # a newer revision supersedes this one; it gets its own delivery
    if claim(store, vid, rev):
        return "process"
    if store.exists(f"evidence/{vid}/rev-{rev}/sent.json"):
        return "duplicate"
    # Claimed but never sent. If the claim is old, the attempt that made it died (timeout, OOM,
    # instance shutdown) without releasing it. Take it over rather than dropping the report.
    now = now or datetime.now(UTC)
    at = _claimed_at(store, vid, rev)
    if at is None or now - at > STALE_CLAIM:
        logger.warning("taking over stale claim for %s rev %s (claimed %s)", vid, rev, at)
        release(store, vid, rev)
        if claim(store, vid, rev):
            return "process"
    return "duplicate"
