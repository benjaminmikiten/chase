import json
from datetime import date
from pathlib import Path

import pytest

from chase_report.app import create_app, process
from tests.fakes import FakeMailer, FakeStore, fake_encode
from tests.test_render import SENDER

FIX = Path(__file__).parent / "fixtures"
V = "v-20261108T124210Z"
NAME = f"violations/{V}/rev-1.json"


def fake_pdf(html: str, out: Path) -> None:
    out.write_bytes(b"%PDF-1.7 fake " + html[:2000].encode())


def store() -> FakeStore:
    return FakeStore.from_dir(FIX / "bucket")


def run(s: FakeStore, mailer: FakeMailer, tmp_path: Path, **kw) -> str:
    return process(
        s,
        NAME,
        SENDER,
        mailer,
        tmp_path,
        today=date(2026, 11, 9),
        limit_mb=15.0,
        encode=kw.get("encode", fake_encode),
        pdf_renderer=kw.get("pdf", fake_pdf),
    )


def test_end_to_end_sends_one_email(tmp_path):
    s, m = store(), FakeMailer()
    assert run(s, m, tmp_path) == "sent"
    assert len(m.sent) == 1
    msg = m.sent[0]
    assert msg["Subject"] == "Barking dog complaint: Nov 8, 2026 (2 episodes)"
    names = [p.get_filename() or "" for p in msg.iter_attachments()]
    assert names[0].endswith(".pdf") and len(names) == 3  # PDF + 2 small MP3s
    assert s.exists(f"evidence/{V}/rev-1/report.pdf")
    sent = json.loads(s.read_bytes(f"evidence/{V}/rev-1/sent.json"))
    assert sent["attachments"] == names and sent["message_id"]
    assert list(tmp_path.iterdir()) == []


def test_redelivery_does_not_resend(tmp_path):
    s, m = store(), FakeMailer()
    run(s, m, tmp_path)
    assert run(s, m, tmp_path) == "duplicate"
    assert len(m.sent) == 1


def test_failure_releases_claim_then_retry_succeeds(tmp_path):
    s, m = store(), FakeMailer()

    def boom(src, dst):
        raise RuntimeError("ffmpeg failed")

    with pytest.raises(RuntimeError):
        run(s, m, tmp_path, encode=boom)
    assert not s.exists(f"evidence/{V}/rev-1/claimed.json")
    assert m.sent == []
    assert run(s, m, tmp_path) == "sent"


def test_missing_episode_no_email(tmp_path):
    s, m = store(), FakeMailer()
    second = sorted(n for n in s.objects if n.startswith("episodes/"))[-1]
    s.delete(second)
    with pytest.raises(Exception, match="not uploaded"):
        run(s, m, tmp_path)
    assert m.sent == [] and not s.exists(f"evidence/{V}/rev-1/claimed.json")


def test_failure_after_send_keeps_claim(tmp_path, monkeypatch):
    s, m = store(), FakeMailer()
    real_put = s.put_bytes

    def put(name, data, content_type, if_absent=False):
        if name.endswith("sent.json"):
            raise OSError("bucket hiccup")
        return real_put(name, data, content_type, if_absent)

    monkeypatch.setattr(s, "put_bytes", put)
    with pytest.raises(OSError):
        run(s, m, tmp_path)
    assert len(m.sent) == 1
    assert s.exists(f"evidence/{V}/rev-1/claimed.json")  # never send twice


def test_links_instead_of_attachments_when_over_budget(tmp_path):
    s, m = store(), FakeMailer()
    out = process(
        s,
        NAME,
        SENDER,
        m,
        tmp_path,
        today=date(2026, 11, 9),
        limit_mb=0.001,
        encode=fake_encode,
        pdf_renderer=fake_pdf,
    )
    assert out == "sent"
    msg = m.sent[0]
    assert (list(msg.iter_attachments())[0].get_filename() or "").endswith(".pdf")
    assert len(list(msg.iter_attachments())) == 1
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None and "audio.mp3" in body.get_content()


def envelope(obj: str, event: str = "OBJECT_FINALIZE") -> dict:
    return {"message": {"attributes": {"bucketId": "bkt", "objectId": obj, "eventType": event}}}


@pytest.fixture
def client(tmp_path):
    s, m = store(), FakeMailer()
    app = create_app(
        store_factory=lambda bucket: s,
        sender_loader=lambda: SENDER,
        mailer_factory=lambda: m,
        workdir=tmp_path,
        encode=fake_encode,
        pdf_renderer=fake_pdf,
    )
    app.config["store"], app.config["mailer"] = s, m
    return app.test_client(), s, m


def test_http_sent_then_duplicate(client):
    c, s, m = client
    assert c.post("/", json=envelope(NAME)).status_code == 204
    assert c.post("/", json=envelope(NAME)).status_code == 204
    assert len(m.sent) == 1


def test_http_ignores_non_manifest(client):
    c, _, m = client
    assert c.post("/", json=envelope("episodes/x/audio.flac")).status_code == 204
    assert c.post("/", json=envelope(NAME, "OBJECT_DELETE")).status_code == 204
    assert m.sent == []


def test_http_malformed_is_400(client):
    c, _, _ = client
    assert c.post("/", data="nope", content_type="text/plain").status_code == 400
    assert c.post("/", json={"hello": 1}).status_code == 400


def test_http_failure_is_500_and_logged(client, capsys):
    c, s, m = client
    second = sorted(n for n in s.objects if n.startswith("episodes/"))[-1]
    s.delete(second)
    assert c.post("/", json=envelope(NAME)).status_code == 500
    logs = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    assert any(r["event"] == "report_failed" and r["severity"] == "ERROR" for r in logs)


def _rev2_store() -> FakeStore:
    s = store()
    raw = json.loads(s.objects[NAME])
    raw["revision"] = 2
    s.objects[f"violations/{V}/rev-2.json"] = json.dumps(raw).encode()
    return s


def test_rev2_without_a_sent_rev1_is_not_called_updated(tmp_path):
    s, m = _rev2_store(), FakeMailer()
    out = process(
        s,
        f"violations/{V}/rev-2.json",
        SENDER,
        m,
        tmp_path,
        today=date(2026, 11, 9),
        encode=fake_encode,
        pdf_renderer=fake_pdf,
    )
    assert out == "sent"
    assert "(updated)" not in m.sent[0]["Subject"]


def test_rev2_after_rev1_was_sent_is_updated(tmp_path):
    s, m = store(), FakeMailer()
    run(s, m, tmp_path)
    raw = json.loads(s.objects[NAME])
    raw["revision"] = 2
    s.objects[f"violations/{V}/rev-2.json"] = json.dumps(raw).encode()
    process(
        s,
        f"violations/{V}/rev-2.json",
        SENDER,
        m,
        tmp_path,
        today=date(2026, 11, 9),
        encode=fake_encode,
        pdf_renderer=fake_pdf,
    )
    assert m.sent[-1]["Subject"].endswith("(updated)")
