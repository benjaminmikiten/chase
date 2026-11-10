import json

from chase_report.trigger import claim, decide, latest_revision, manifest_key, parse_push
from tests.fakes import FakeStore

V = "v-20261108T124210Z"


def envelope(obj: str, event: str = "OBJECT_FINALIZE") -> dict:
    return {
        "message": {
            "attributes": {"bucketId": "b", "objectId": obj, "eventType": event},
            "data": "",
            "messageId": "1",
        },
        "subscription": "projects/p/subscriptions/s",
    }


def store_with(*revs: int) -> FakeStore:
    return FakeStore({f"violations/{V}/rev-{r}.json": json.dumps({"r": r}).encode() for r in revs})


def test_parse_push_finalize():
    assert parse_push(envelope(f"violations/{V}/rev-1.json")) == ("b", f"violations/{V}/rev-1.json")


def test_parse_push_other_events_ignored():
    assert parse_push(envelope("x", "OBJECT_DELETE")) is None
    assert parse_push({"nope": 1}) is None


def test_manifest_key():
    assert manifest_key(f"violations/{V}/rev-12.json") == (V, 12)
    assert manifest_key("episodes/ep-1/metadata.json") is None
    assert manifest_key(f"violations/{V}/rev-1.json.tmp") is None


def test_non_manifest_ignored():
    assert decide(store_with(1), "episodes/ep-1/audio.flac") == "ignore"


def test_latest_revision():
    assert latest_revision(store_with(1, 2, 10), V) == 10


def test_stale_revision_ignored():
    assert decide(store_with(1, 2), f"violations/{V}/rev-1.json") == "stale"


def test_second_delivery_is_duplicate():
    s = store_with(1)
    assert decide(s, f"violations/{V}/rev-1.json") == "process"
    assert decide(s, f"violations/{V}/rev-1.json") == "duplicate"
    assert s.exists(f"evidence/{V}/rev-1/claimed.json")


def test_claim_once():
    s = store_with(1)
    assert claim(s, V, 1) is True
    assert claim(s, V, 1) is False


def _claim_at(s: FakeStore, minutes_ago: float, now) -> None:
    from datetime import timedelta

    at = (now - timedelta(minutes=minutes_ago)).isoformat()
    s.objects[f"evidence/{V}/rev-1/claimed.json"] = json.dumps({"claimed_at": at}).encode()


def test_stale_claim_without_sent_is_taken_over():
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    s = store_with(1)
    _claim_at(s, 20, now)  # a crashed/killed attempt left this behind
    assert decide(s, f"violations/{V}/rev-1.json", now=now) == "process"


def test_fresh_claim_is_duplicate():
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    s = store_with(1)
    _claim_at(s, 5, now)
    assert decide(s, f"violations/{V}/rev-1.json", now=now) == "duplicate"


def test_old_claim_with_sent_is_duplicate():
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    s = store_with(1)
    _claim_at(s, 600, now)
    s.objects[f"evidence/{V}/rev-1/sent.json"] = b"{}"
    assert decide(s, f"violations/{V}/rev-1.json", now=now) == "duplicate"
