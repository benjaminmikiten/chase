"""The report: a plain complaint letter (page 1) followed by the evidence packet.

Wording rules (design §7 and Ben's decisions):
- barking minutes is the headline number;
- the count is always "barking bursts", never "barks";
- no single address is named as the source;
- the preset's citation and caveat are printed;
- audio gaps are disclosed;
- both hashes are listed;
- all times are local.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, select_autoescape

from chase_report.assemble import Evidence
from chase_report.mail import Sender
from chase_report.model import EpisodeMeta, Manifest

_env = Environment(
    loader=PackageLoader("chase_report", "templates"),
    autoescape=select_autoescape(["html.j2"], default_for_string=False, default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _clock(d: datetime) -> str:
    h = d.hour % 12 or 12
    return f"{h}:{d:%M:%S} {'AM' if d.hour < 12 else 'PM'}"


def _short_clock(d: datetime) -> str:
    h = d.hour % 12 or 12
    return f"{h}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


def _long_date(d: date) -> str:
    return f"{d:%A}, {d:%B} {d.day}, {d.year}"


def _duration(seconds: float) -> str:
    m, s = divmod(round(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


@dataclass(frozen=True)
class EpisodeRow:
    number: int
    date_local: str
    start_local: str
    end_local: str
    duration: str
    barking_minutes: str
    bursts: int
    longest_gap: str
    loudness: str
    audio_status: str
    flac_sha: str
    mp3_sha: str
    timeline_svg: str


@dataclass(frozen=True)
class ReportView:
    title: str
    reference: str
    report_date: str
    addressee: str
    day: date
    headline: str
    total_barking_minutes: int
    episodes: tuple[EpisodeRow, ...]
    source_line: str
    rule_text: str
    citation: str
    caveat: str
    hourly_svg: str
    recording: tuple[tuple[str, str], ...]
    sender: Sender
    links: tuple[tuple[str, str], ...]
    timezone: str
    test: bool = False


def timeline_svg(meta: EpisodeMeta, width: int = 700, height: int = 46) -> str:
    """Barking (red) vs quiet across the episode, with missing audio hatched, and local times."""
    tz = ZoneInfo(meta.timezone)
    span = max((meta.end - meta.start).total_seconds(), 1.0)

    def x(d: datetime) -> float:
        return round(max(0.0, min(1.0, (d - meta.start).total_seconds() / span)) * width, 2)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" class="timeline">',
        f'<rect x="0" y="4" width="{width}" height="22" fill="#f1f1f1" stroke="#bbb"/>',
    ]
    for w in meta.windows:
        x0, x1 = x(w.start), x(w.end)
        parts.append(
            f'<rect class="bark" x="{x0}" y="4" width="{max(x1 - x0, 0.8)}" height="22" '
            f'fill="#c0392b"/>'
        )
    for g in meta.gaps:
        x0, x1 = x(g.start), x(g.end)
        parts.append(
            f'<rect class="gap" x="{x0}" y="1" width="{max(x1 - x0, 3.0)}" height="28" '
            f'fill="#222" stroke="#f1c40f" stroke-width="1"/>'
        )
    for frac, anchor in ((0.0, "start"), (0.5, "middle"), (1.0, "end")):
        t = meta.start + (meta.end - meta.start) * frac
        parts.append(
            f'<text x="{round(frac * width, 1)}" y="40" font-size="10" fill="#666" '
            f'text-anchor="{anchor}">{escape(_short_clock(t.astimezone(tz)))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def hourly_svg(manifest: Manifest, day: date, timezone: str, width: int = 700) -> str:
    """Barking bursts per local hour on `day` (bars) vs the average for each hour (dashed)."""
    tz = ZoneInfo(timezone)
    today: Counter[int] = Counter()
    others: Counter[int] = Counter()
    other_days: set[date] = set()
    for hour_start, n in manifest.hourly_history:
        lt = hour_start.astimezone(tz)
        if lt.date() == day:
            today[lt.hour] += n
        else:
            others[lt.hour] += n
            other_days.add(lt.date())
    avg = {h: others[h] / len(other_days) if other_days else 0.0 for h in range(24)}
    top = max([*today.values(), *avg.values(), 1])
    height, base, bar_w = 120, 100, width / 24

    def y(v: float) -> float:
        return round(base - (v / top) * (base - 6), 2)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" class="hourly">'
    ]
    for h in range(24):
        v = today.get(h, 0)
        parts.append(
            f'<rect class="bar" x="{round(h * bar_w + 2, 2)}" y="{y(v)}" '
            f'width="{round(bar_w - 4, 2)}" height="{round(base - y(v), 2)}" '
            f'fill="{"#c0392b" if v > avg[h] * 2 and v > 0 else "#9aa5b1"}"/>'
        )
        if h % 3 == 0:
            label = f"{h % 12 or 12}{'a' if h < 12 else 'p'}"
            parts.append(
                f'<text x="{round(h * bar_w + bar_w / 2, 2)}" y="114" font-size="9" '
                f'fill="#666" text-anchor="middle">{label}</text>'
            )
    points = " ".join(f"{round(h * bar_w + bar_w / 2, 2)},{y(avg[h])}" for h in range(24))
    parts.append(
        f'<polyline class="avg" points="{points}" fill="none" stroke="#e67e22" '
        f'stroke-width="1.5" stroke-dasharray="5,4"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


def build_view(
    evidence: Evidence, sender: Sender, links: dict[str, str], today: date
) -> ReportView:
    m = evidence.manifest
    tzname = evidence.episodes[0].meta.timezone if evidence.episodes else "UTC"
    tz = ZoneInfo(tzname)
    rows: list[EpisodeRow] = []
    for i, e in enumerate(evidence.episodes, 1):
        meta = e.meta
        start, end = meta.start.astimezone(tz), meta.end.astimezone(tz)
        if meta.complete:
            status = "complete"
        else:
            status = (
                "; ".join(
                    f"missing {round(g.seconds)} s at {_clock(g.start.astimezone(tz))}"
                    for g in meta.gaps
                )
                or "incomplete"
            )
        rows.append(
            EpisodeRow(
                number=i,
                date_local=f"{start:%a}, {start:%b} {start.day}",
                start_local=_clock(start),
                end_local=_clock(end),
                duration=_duration(meta.duration),
                barking_minutes=f"{meta.barking_seconds / 60:.1f}",
                bursts=meta.bursts,
                longest_gap=f"{round(meta.longest_gap_seconds)} s",
                loudness=f"{meta.mean_loudness_dbfs:.1f} dBFS",
                audio_status=status,
                flac_sha=meta.flac_sha256,
                mp3_sha=e.mp3_sha256,
                timeline_svg=timeline_svg(meta),
            )
        )

    starts = [e.meta.start.astimezone(tz).date() for e in evidence.episodes]
    first, last = min(starts), max(starts)
    total = round(sum(e.meta.barking_seconds for e in evidence.episodes) / 60)
    n = len(rows)
    when = (
        f"On {_long_date(first)}"
        if first == last
        else f"Between {_long_date(first)} and {_long_date(last)}"
    )
    headline = (
        f"{when}, {n} separate episode{'s' if n != 1 else ''} of sustained dog barking were "
        f"recorded within {round(m.rule.violation_window_hours)} hours, totalling {total} "
        f"minutes of barking."
    )
    addrs = ", ".join(sender.neighbor_addresses)
    source = "from neighboring properties" + (f", including: {addrs}" if addrs else "")
    r = m.rule
    rule_text = (
        f"An episode is at least {r.episode_min_minutes:g} minutes of barking with no pause "
        f"longer than {r.max_gap_seconds:g} seconds (episodes less than "
        f"{r.min_separation_minutes:g} minutes apart count as one). This report is generated "
        f"when {r.episodes_per_window} or more episodes occur within "
        f"{r.violation_window_hours:g} hours."
    )
    meta0 = evidence.episodes[0].meta if evidence.episodes else None
    recording = (
        ("Device", meta0.device if meta0 else "unknown"),
        ("Location", "fixed microphone on the reporting resident's property"),
        (
            "Detector",
            f"{meta0.model if meta0 else 'YAMNet'} (Google AudioSet classifier), "
            f"dog-sound score ≥ {r.confidence_threshold:g}",
        ),
        ("Clock", f"{meta0.clock_source if meta0 else 'unknown'}; times shown in {tzname}"),
        ("Audio", "lossless FLAC originals retained; MP3 copies for convenience"),
    )
    return ReportView(
        title="Barking Dog Noise Complaint",
        reference=f"CHASE-{m.violation_id[2:]}-R{m.revision}",
        report_date=_long_date(today),
        addressee=r.addressee,
        day=first,
        headline=headline,
        total_barking_minutes=total,
        episodes=tuple(rows),
        source_line=source,
        rule_text=rule_text,
        citation=r.citation,
        caveat=r.caveat,
        hourly_svg=hourly_svg(m, first, tzname),
        recording=recording,
        sender=sender,
        links=tuple(links.items()),
        timezone=tzname,
        test=m.test,
    )


def render_html(view: ReportView) -> str:
    return _env.get_template("report.html.j2").render(v=view)


def letter_text(view: ReportView) -> str:
    return _env.get_template("letter.txt.j2").render(v=view).strip() + "\n"


def render_pdf(html: str, out: Path) -> None:
    from weasyprint import HTML  # imported lazily: needs pango system libraries

    HTML(string=html).write_pdf(str(out))
