import json
import re
from datetime import date
from pathlib import Path

import pytest

from chase_report.assemble import EpisodeEvidence, Evidence
from chase_report.mail import Sender
from chase_report.model import EpisodeMeta, Manifest
from chase_report.render import build_view, hourly_svg, letter_text, render_html, timeline_svg

FIX = Path(__file__).parent / "fixtures"
SENDER = Sender(
    name="Pat Example",
    address="123 Sample St, Example City, TX",
    phone="(555) 010-0000",
    email="pat@example.com",
    to_email="pat@example.com",
    neighbor_addresses=("125 Sample St", "200 Other Ave"),
)


def evidence() -> Evidence:
    m = Manifest.from_json(json.loads((FIX / "violation_manifest.json").read_text()))
    eps = []
    for eid in m.episode_ids:
        meta = EpisodeMeta.from_json(
            json.loads((FIX / "bucket" / "episodes" / eid / "metadata.json").read_text())
        )
        base = f"evidence/{m.violation_id}/episodes/{eid}/"
        eps.append(EpisodeEvidence(meta, base + "audio.flac", base + "audio.mp3", "b" * 64, 1000))
    return Evidence(m, tuple(eps))


LINKS = {"ep-20261108T124210Z/audio.flac": "https://signed.example/a"}


def view():
    return build_view(evidence(), SENDER, LINKS, date(2026, 11, 9))


def test_headline_uses_minutes_and_local_date():
    v = view()
    assert "Sunday, November 8, 2026" in v.headline
    assert "2 separate episodes" in v.headline
    assert re.search(r"\d+ minutes", v.headline)


def test_never_says_barks():
    v = view()
    for text in (render_html(v), letter_text(v)):
        assert "barking bursts" in text
        assert not re.search(r"\bbarks\b", text, re.IGNORECASE)


def test_no_single_address_named_as_source():
    v = view()
    assert v.source_line.startswith("from neighboring properties")
    assert "125 Sample St" in v.source_line and "200 Other Ave" in v.source_line


def test_caveat_and_citation_present():
    html = render_html(view())
    assert "twenty-four (24) hours" in html
    assert "unverified" in html


def test_gaps_disclosed():
    v = view()
    assert v.episodes[0].audio_status == "complete"
    assert "12 s" in v.episodes[1].audio_status and "10:18" in v.episodes[1].audio_status
    assert "12 s" in render_html(v)


def test_both_hashes_listed():
    v = view()
    html = render_html(v)
    assert v.episodes[0].flac_sha in html
    assert "b" * 64 in html


def test_times_are_local():
    v = view()
    assert v.episodes[0].start_local == "6:42:10 AM"
    assert v.episodes[1].start_local == "10:15:02 PM"


def test_letter_has_sender_and_addressee():
    text = letter_text(view())
    assert "San Antonio Animal Care Services" in text
    assert "Pat Example" in text and "(555) 010-0000" in text


def test_timeline_svg_marks_windows():
    ev = evidence()
    svg = timeline_svg(ev.episodes[0].meta)
    assert svg.startswith("<svg") and svg.count('class="bark"') > 50


def test_hourly_svg_has_24_bars_and_average():
    ev = evidence()
    svg = hourly_svg(ev.manifest, date(2026, 11, 8), "America/Chicago")
    assert svg.count('class="bar"') == 24
    assert 'class="avg"' in svg


def test_html_escapes_user_text():
    s = Sender(
        name="<script>x</script>",
        address="a",
        phone="p",
        email="e@example.com",
        to_email="e@example.com",
        neighbor_addresses=(),
    )
    html = render_html(build_view(evidence(), s, {}, date(2026, 11, 9)))
    assert "<script>x</script>" not in html


@pytest.mark.pdf
def test_render_pdf_real(tmp_path):
    try:
        import weasyprint  # noqa: F401
    except OSError:
        pytest.skip("WeasyPrint system libraries not installed")
    from chase_report.render import render_pdf

    out = tmp_path / "r.pdf"
    render_pdf(render_html(view()), out)
    assert out.read_bytes()[:4] == b"%PDF" and out.stat().st_size > 5000
