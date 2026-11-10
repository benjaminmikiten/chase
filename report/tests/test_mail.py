from datetime import date
from pathlib import Path

from chase_report.mail import Sender, attach_audio, compose
from tests.test_render import SENDER, view


def pdf(tmp_path: Path, size: int = 50_000) -> Path:
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF" + b"x" * size)
    return p


def mp3(tmp_path: Path, name: str, size: int) -> tuple[str, Path]:
    p = tmp_path / name
    p.write_bytes(b"ID3" + b"x" * size)
    return name, p


LINKS = {
    "Episode 1 (FLAC)": "https://signed.example/1.flac",
    "Episode 1 (MP3)": "https://signed.example/1.mp3",
}


def test_subject_first_report():
    msg = compose(view(), Path("/dev/null"), [], LINKS, SENDER, updated=False)
    assert msg["Subject"] == "Barking dog complaint: Nov 8, 2026 (2 episodes)"
    assert msg["From"] == "pat@example.com" and msg["To"] == "pat@example.com"


def test_subject_updated_on_rev2():
    msg = compose(view(), Path("/dev/null"), [], LINKS, SENDER, updated=True)
    assert msg["Subject"].endswith("(2 episodes) (updated)")


def test_body_is_the_letter_with_links(tmp_path):
    msg = compose(view(), pdf(tmp_path), [], LINKS, SENDER, updated=False)
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None
    text = body.get_content()
    assert "San Antonio Animal Care Services" in text
    assert "https://signed.example/1.flac" in text
    assert "expire" in text


def test_attach_mp3_when_under_budget(tmp_path):
    files = [mp3(tmp_path, "episode-1.mp3", 1_000_000)]
    msg = compose(view(), pdf(tmp_path), files, LINKS, SENDER, updated=False)
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert names == ["report.pdf", "episode-1.mp3"]


def test_attach_audio_budget_counts_base64():
    mb = 1024 * 1024
    assert attach_audio(1 * mb, [10 * mb], limit_encoded_mb=15.0) is True
    # 14 MB raw is ~18.7 MB once base64-encoded: over a 15 MB encoded budget.
    assert attach_audio(1 * mb, [13 * mb], limit_encoded_mb=15.0) is False


def test_links_when_over_budget(tmp_path):
    msg = compose(view(), pdf(tmp_path), [], LINKS, SENDER, updated=False)
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert names == ["report.pdf"]
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None and "https://signed.example/1.mp3" in body.get_content()


def test_sender_from_json():
    s = Sender.from_json(
        '{"name":"A","address":"B","phone":"C","email":"d@example.com",'
        '"to_email":"e@example.com","neighbor_addresses":["X","Y"]}'
    )
    assert s.neighbor_addresses == ("X", "Y") and s.to_email == "e@example.com"


def test_view_date_used_in_subject():
    v = view()
    assert v.day == date(2026, 11, 8)


def test_test_manifest_subject_and_banner():
    import json
    from dataclasses import replace

    from chase_report.model import Manifest

    raw = json.loads((Path(__file__).parent / "fixtures" / "violation_manifest.json").read_text())
    raw["test"] = True
    assert Manifest.from_json(raw).test is True
    v = replace(view(), test=True)
    msg = compose(v, Path("/dev/null"), [], {}, SENDER, updated=False)
    assert msg["Subject"].startswith("[TEST] Barking dog complaint")
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None and "TEST REPORT" in body.get_content()


def test_smtp_quit_error_after_send_counts_as_sent(monkeypatch):
    import smtplib

    from chase_report.mail import SmtpSender

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            calls["timeout"] = timeout

        def login(self, u, p):
            pass

        def send_message(self, msg):
            calls["sent"] = True

        def quit(self):
            raise smtplib.SMTPServerDisconnected("bye")

        def close(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    SmtpSender("u", "p").send(compose(view(), Path("/dev/null"), [], {}, SENDER, updated=False))
    assert calls == {"timeout": 60, "sent": True}


def test_smtp_send_failure_raises(monkeypatch):
    import smtplib

    import pytest

    from chase_report.mail import SmtpSender

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            raise smtplib.SMTPRecipientsRefused({})

        def quit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        SmtpSender("u", "p").send(compose(view(), Path("/dev/null"), [], {}, SENDER, updated=False))


def test_sender_timezone_default_and_value():
    assert (
        Sender.from_json(
            '{"name":"A","address":"B","phone":"C","email":"d@e.com","to_email":"d@e.com"}'
        ).timezone
        == "UTC"
    )
    assert (
        Sender.from_json(
            '{"name":"A","address":"B","phone":"C","email":"d@e.com","to_email":"d@e.com","timezone":"America/Chicago"}'
        ).timezone
        == "America/Chicago"
    )
