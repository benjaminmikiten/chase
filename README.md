# chase

> No job's too big, no pup's too loud.

`chase` listens for dog barking through a USB microphone, charts it on a Grafana Cloud dashboard, and when barking crosses a configurable noise-ordinance threshold, packages the audio and a ready-to-forward PDF complaint.

**Status:** Plan 1 (listener + dashboard) is built. Plan 2 (cloud upload, PDF report and email, alerting) is next.

- Design: [`docs/superpowers/specs/2026-11-09-chase-design.md`](docs/superpowers/specs/2026-11-09-chase-design.md)
- Run it: [`docs/running-on-the-optiplex.md`](docs/running-on-the-optiplex.md)
- Decisions: [`docs/adr/`](docs/adr/)

## How it works

```
USB mic -> 0.96 s windows -> YAMNet "dog/bark" score -> SQLite bark log
                                                            |
                         pure rule engine (episodes, violations; config-driven)
                                |                         |                       |
             evidence outbox (FLAC + metadata,   Prometheus metrics      JSON events
             violation manifests)                       \                     /
                                                         Grafana Alloy -> Grafana Cloud dashboard
```

- **Episode:** at least 10 minutes of barking with no gap over 30 seconds. Episodes less than
  10 minutes apart merge into one.
- **Violation:** at least 2 episodes within 24 hours (San Antonio preset).
- Every threshold is config. `chase replay` re-runs the rules over your history so you can tune
  them before changing anything live.

### A note on the ordinance

San Antonio's animal code bans letting animals create a noise disturbance but sets no
per-event duration. The "two recorded events in 24 hours" rule comes from a **2017 draft** of
Chapter 5 and is **unverified** against the current code. The 10-minute episode definition
follows Los Angeles practice. Confirm with 311 before relying on any of it, and add your own
city as a preset in `config/presets/`.

## Metrics

| Metric | Meaning |
|---|---|
| `chase_barking_windows_total`, `chase_barks_total` | barking windows; estimated **barking bursts** (not individual barks; see ADR 0002) |
| `chase_current_run_seconds`, `chase_seconds_until_episode` | barking in progress |
| `chase_episodes_total`, `chase_violations_total`, `chase_episodes_in_window` | rule outcomes |
| `chase_barks_this_hour`, `chase_typical_barks_this_hour`, `chase_barks_today`, `chase_daily_average_barks`, `chase_heatmap_barks` | 30-day baselines (computed locally) |
| `chase_heartbeat_timestamp_seconds`, `chase_audio_errors_total`, `chase_outbox_pending` | health |

## Development

```bash
cd listener
uv sync
uv run pytest            # unit tests
uv run ruff check && uv run ruff format --check && uv run pyright
```
