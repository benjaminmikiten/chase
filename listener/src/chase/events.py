"""Structured JSON log events (one line each), shipped to Loki by Alloy."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any


def emit(event: str, **fields: Any) -> None:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    sys.stdout.write(json.dumps({"ts": ts, "event": event, **fields}, default=str) + "\n")
    sys.stdout.flush()
