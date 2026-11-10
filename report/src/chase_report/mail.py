"""The ready-to-forward email: the letter as body, the PDF attached, audio attached or linked."""

from __future__ import annotations

import json
import math
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chase_report.render import ReportView

MB = 1024 * 1024


@dataclass(frozen=True)
class Sender:
    """Personal details for the letter, from the REPORT_CONFIG secret (never in the repo)."""

    name: str
    address: str
    phone: str
    email: str
    to_email: str
    neighbor_addresses: tuple[str, ...]

    @classmethod
    def from_json(cls, raw: str) -> Sender:
        d = json.loads(raw)
        return cls(
            name=d["name"],
            address=d["address"],
            phone=d["phone"],
            email=d["email"],
            to_email=d["to_email"],
            neighbor_addresses=tuple(d.get("neighbor_addresses", [])),
        )


def attach_audio(pdf_bytes: int, mp3_bytes: list[int], limit_encoded_mb: float = 15.0) -> bool:
    """True if the PDF plus every MP3 fits the budget once base64-encoded (4/3 larger).

    The budget is deliberately below Gmail's 25 MB limit: the email gets forwarded, and the
    recipient's limit is unknown.
    """
    encoded = sum(math.ceil(n * 4 / 3) for n in [pdf_bytes, *mp3_bytes])
    return encoded <= limit_encoded_mb * MB


def subject(view: ReportView, revision: int) -> str:
    d = view.day
    n = len(view.episodes)
    s = f"Barking dog complaint: {d:%b} {d.day}, {d.year} ({n} episode{'s' if n != 1 else ''})"
    return s + (" (updated)" if revision > 1 else "")


def compose(
    view: ReportView,
    pdf: Path,
    mp3s: list[tuple[str, Path]],
    links: dict[str, str],
    sender: Sender,
    revision: int,
) -> EmailMessage:
    from chase_report.render import letter_text

    body = letter_text(view)
    if links:
        lines = [f"- {label}: {url}" for label, url in links.items()]
        body += "\n\nRecordings (these download links expire in 7 days):\n" + "\n".join(lines)
    if revision > 1:
        body = (
            "This updated report replaces the earlier one for the same period; it adds the "
            "episodes recorded since.\n\n" + body
        )

    msg = EmailMessage()
    msg["Subject"] = subject(view, revision)
    msg["From"] = sender.email
    msg["To"] = sender.to_email
    msg["Message-ID"] = make_msgid(domain="chase.local")
    msg.set_content(body)
    if pdf.exists() and pdf.stat().st_size:
        msg.add_attachment(
            pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name
        )
    for name, path in mp3s:
        msg.add_attachment(path.read_bytes(), maintype="audio", subtype="mpeg", filename=name)
    return msg


class SmtpSender:  # pragma: no cover - real network; exercised by the morning TEST report
    def __init__(self, user: str, app_password: str, host: str = "smtp.gmail.com", port: int = 465):
        self.user, self.password, self.host, self.port = user, app_password, host, port

    def send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP_SSL(self.host, self.port, context=ssl.create_default_context()) as s:
            s.login(self.user, self.password)
            s.send_message(msg)
