"""In-memory stand-ins for GCS and SMTP."""

from __future__ import annotations

import shutil
from email.message import EmailMessage
from pathlib import Path


class FakeStore:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.copies: list[tuple[str, str]] = []

    @classmethod
    def from_dir(cls, root: Path) -> FakeStore:
        return cls(
            {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        )

    def read_bytes(self, name: str) -> bytes:
        if name not in self.objects:
            raise FileNotFoundError(name)
        return self.objects[name]

    def download(self, name: str, path: Path) -> None:
        path.write_bytes(self.read_bytes(name))

    def upload(self, name: str, path: Path, content_type: str) -> None:
        self.objects[name] = path.read_bytes()

    def put_bytes(self, name: str, data: bytes, content_type: str, if_absent: bool = False) -> bool:
        if if_absent and name in self.objects:
            return False
        self.objects[name] = data
        return True

    def delete(self, name: str) -> None:
        self.objects.pop(name, None)

    def copy(self, src: str, dst: str) -> None:
        self.objects[dst] = self.read_bytes(src)
        self.copies.append((src, dst))

    def list(self, prefix: str) -> list[str]:
        return sorted(n for n in self.objects if n.startswith(prefix))

    def exists(self, name: str) -> bool:
        return name in self.objects

    def signed_url(self, name: str, hours: int) -> str:
        return f"https://signed.example/{name}?hours={hours}"


class FakeMailer:
    def __init__(self, fail: bool = False):
        self.sent: list[EmailMessage] = []
        self.fail = fail

    def send(self, msg: EmailMessage) -> None:
        if self.fail:
            raise OSError("smtp down")
        self.sent.append(msg)


def fake_encode(src: Path, dst: Path) -> None:
    shutil.copy(src, dst)
