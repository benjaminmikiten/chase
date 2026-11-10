"""Object storage: a small protocol, the GCS implementation, and nothing else.

Tests use tests/fakes.py:FakeStore. Never call GCS from tests.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol


class Store(Protocol):
    def read_bytes(self, name: str) -> bytes: ...
    def download(self, name: str, path: Path) -> None: ...
    def upload(self, name: str, path: Path, content_type: str) -> None: ...
    def put_bytes(
        self, name: str, data: bytes, content_type: str, if_absent: bool = False
    ) -> bool: ...
    def delete(self, name: str) -> None: ...
    def copy(self, src: str, dst: str) -> None: ...
    def list(self, prefix: str) -> list[str]: ...
    def exists(self, name: str) -> bool: ...
    def signed_url(self, name: str, hours: int) -> str: ...


class GcsStore:  # pragma: no cover - talks to real GCS; exercised by the morning smoke test
    def __init__(self, bucket: str):
        import google.auth
        from google.cloud import storage

        self._creds, project = google.auth.default()
        self._client = storage.Client(project=project, credentials=self._creds)
        self._bucket = self._client.bucket(bucket)

    def read_bytes(self, name: str) -> bytes:
        return self._bucket.blob(name).download_as_bytes()

    def download(self, name: str, path: Path) -> None:
        self._bucket.blob(name).download_to_filename(str(path))

    def upload(self, name: str, path: Path, content_type: str) -> None:
        self._bucket.blob(name).upload_from_filename(str(path), content_type=content_type)

    def put_bytes(self, name: str, data: bytes, content_type: str, if_absent: bool = False) -> bool:
        from google.api_core.exceptions import PreconditionFailed

        kwargs: dict[str, Any] = {"if_generation_match": 0} if if_absent else {}
        try:
            self._bucket.blob(name).upload_from_string(data, content_type=content_type, **kwargs)
        except PreconditionFailed:
            return False
        return True

    def delete(self, name: str) -> None:
        from google.api_core.exceptions import NotFound

        with contextlib.suppress(NotFound):
            self._bucket.blob(name).delete()

    def copy(self, src: str, dst: str) -> None:
        self._bucket.copy_blob(self._bucket.blob(src), self._bucket, dst)

    def list(self, prefix: str) -> list[str]:
        return sorted(b.name for b in self._client.list_blobs(self._bucket, prefix=prefix))

    def exists(self, name: str) -> bool:
        return self._bucket.blob(name).exists()

    def signed_url(self, name: str, hours: int) -> str:
        # On Cloud Run there's no private key: sign through IAM (signBlob) as the service's own
        # account, which holds roles/iam.serviceAccountTokenCreator on itself (Terraform).
        from google.auth.transport.requests import Request

        self._creds.refresh(Request())
        return self._bucket.blob(name).generate_signed_url(
            version="v4",
            expiration=timedelta(hours=hours),
            method="GET",
            service_account_email=getattr(self._creds, "service_account_email", None),
            access_token=self._creds.token,
        )
