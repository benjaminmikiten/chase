"""Render a sample report and email from the synthetic fixtures (no personal data).

    python scripts/render_sample.py OUT_DIR

Writes sample-report.pdf and sample-email.eml. Needs WeasyPrint's libraries and ffmpeg (run it in
the report image).
"""

from __future__ import annotations

import sys
from datetime import date
from email import policy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chase_report.app import process  # noqa: E402
from tests.fakes import FakeMailer, FakeStore  # noqa: E402
from tests.test_render import SENDER  # noqa: E402


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    store = FakeStore.from_dir(ROOT / "tests" / "fixtures" / "bucket")
    mailer = FakeMailer()
    work = out / "work"
    work.mkdir(exist_ok=True)
    name = next(n for n in store.objects if n.startswith("violations/"))
    assert process(store, name, SENDER, mailer, work, today=date(2026, 11, 9)) == "sent"
    work.rmdir()
    pdf = next(n for n in store.objects if n.endswith("report.pdf"))
    (out / "sample-report.pdf").write_bytes(store.objects[pdf])
    (out / "sample-email.eml").write_bytes(mailer.sent[0].as_bytes(policy=policy.SMTP))
    print(f"wrote {out / 'sample-report.pdf'} and {out / 'sample-email.eml'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
