# chase Plan 1: Listener + Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A containerized listener on the OptiPlex that detects dog barking, logs it, detects episodes and violations under a tunable rule, writes evidence (FLAC + metadata + violation manifests) to a local outbox, and feeds a Grafana Cloud dashboard through Alloy.

**Architecture:** Audio from the USB mic is spooled to disk in 1-minute segments and scored in 0.96 s windows by YAMNet. Barking windows go into a SQLite bark log. A **pure rule engine** (no I/O) turns the log into episodes and violations. The service loop persists what the engine finds, writes evidence to `data/outbox/`, and exports Prometheus metrics and JSON log events. Plan 2 (cloud) adds the uploader that ships the outbox to GCS, plus the report service, Terraform, and alerting.

**Tech Stack:** Python 3.12, uv, pytest, ruff, pyright, numpy, scipy (`resample_poly`), sounddevice, soundfile, ai-edge-litert (YAMNet TFLite), prometheus-client, PyYAML, Docker Compose, Grafana Alloy.

**Spec:** `docs/superpowers/specs/2026-11-09-chase-design.md` (§§3.1, 4, 5, 6, 9). Out of scope for this plan: GCS upload, Cloud Run report/email, Terraform, Grafana alert rules (all Plan 2).

## Global Constraints

- Public repo: never commit personal data (addresses, coordinates, emails, project IDs, recordings, reports). Tests use synthetic audio only, never real recordings of neighbors.
- All rule thresholds come from config/presets; none are hard-coded. Defaults (San Antonio preset): `confidence_threshold: 0.7`, `episode_min_minutes: 10`, `max_gap_seconds: 30`, `min_separation_minutes: 10`, `episodes_per_window: 2`, `violation_window_hours: 24`.
- Pre/post-roll: 30 s each side of an episode.
- Timestamps stored in UTC; rendered in `America/Chicago` (configurable `timezone`).
- Audio leaves the machine only as part of an episode. Backlog clips (v2) are local only, pruned after 90 days (`backlog_days: 90`).
- The rule engine (`rules.py`) is pure: no I/O, no clock reads, no global state. `now` is always a parameter.
- Tests never touch the network, a real mic, or the real YAMNet model (one opt-in integration test may use the model if it's present locally; it's skipped otherwise).
- Conventional commits; one branch + PR per task (GitHub issue per task).
- ADRs in `docs/adr/NNNN-title.md` for: bark-count method (Task 3), OptiPlex key exception (Plan 2), and restart/idempotency approach (Task 10).

## Review Focus

1. **Listener restarts mid-episode** (container update, power blip): no episode lost, none double-counted, no duplicate violation revision. Test in Task 10.
2. **Episode longer than the audio spool** (e.g. a 90-minute bout): the spool must not prune audio an open run still needs; the clip must cover the whole episode plus pre/post-roll. Test in Task 7.
3. **DST change and midnight boundaries:** hourly/daily baselines and "today" bucket by local time (`America/Chicago`) correctly across the fall-back hour. Test in Task 5.
4. **Mic disconnects or returns no data:** the loop must not crash-loop silently. It records `chase_audio_errors_total{reason="capture"}`, keeps the heartbeat honest (stops advancing while no audio arrives), and resumes when audio returns. Test in Task 10.
5. **Out-of-order or overlapping windows / clock jump** (NTP step backwards): the rules sort by start and tolerate overlap without negative gaps or crashes. Test in Task 3.

---

## File Structure

```
listener/
  pyproject.toml                 uv project "chase-listener", package chase, console script `chase`
  Dockerfile
  src/chase/
    __init__.py
    config.py                    Settings + RuleConfig; YAML loading; preset merge; validation
    model.py                     BarkWindow, Episode, Violation, RunStatus dataclasses
    rules.py                     PURE: find_episodes, finalized, current_run, assign_violations, count_bursts
    store.py                     BarkLog (SQLite): windows, episodes, violation revisions, aggregates
    baselines.py                 30-day hourly/daily/heatmap baselines + today/this-hour counts
    detector.py                  YAMNet wrapper: class selection, resample, score, loudness
    audio.py                     Capture (sounddevice) + Spool (1-min segments on disk) + clip assembly
    evidence.py                  outbox writers: episode FLAC+metadata, violation manifests; backlog clips
    metrics.py                   Prometheus metrics (private registry)
    events.py                    structured JSON log events
    service.py                   the loop: windows → store → rules → evidence/metrics/events
    cli.py                       `chase run`, `chase replay`
  tests/                         mirrors src/chase/*.py as test_*.py; tests/conftest.py synthetic fixtures
config/
  chase.example.yaml             committed; real config/chase.yaml is git-ignored
  presets/san-antonio.yaml
compose.yaml
alloy/config.alloy
grafana/dashboard.json
.env.example
.github/workflows/ci.yml
docs/adr/
```

---

### Task 1: Scaffold, tooling, CI

**Files:**
- Create: `listener/pyproject.toml`, `listener/src/chase/__init__.py`, `listener/tests/test_smoke.py`, `.github/workflows/ci.yml`, `docs/adr/0001-record-architecture-decisions.md`
- Modify: `README.md` (add "Development" section: `cd listener && uv sync && uv run pytest`)

**Interfaces:**
- Produces: `chase.__version__ == "0.1.0"`; console script `chase = "chase.cli:main"` (cli.py arrives in Task 11; the entry point is declared now).

- [ ] **Step 1: Write the failing test** `listener/tests/test_smoke.py`:

```python
import chase

def test_version():
    assert chase.__version__ == "0.1.0"
```

- [ ] **Step 2: Run** `cd listener && uv run pytest` → FAIL (no package).
- [ ] **Step 3: Create the `pyproject.toml`.** Use the hatchling build backend with `src/` layout and `requires-python = ">=3.12"`. Runtime deps: numpy, scipy, sounddevice, soundfile, prometheus-client, pyyaml, ai-edge-litert. Dev group: pytest, pytest-cov, ruff, pyright. Ruff: line length 100, rules `E,F,I,UP,B,SIM`. Pyright `standard` mode. Pytest: `testpaths=["tests"]`, plus a marker `integration` deselected by default (`-m "not integration"`). Then create `__init__.py` with `__version__`.
- [ ] **Step 4: Run** `uv run pytest && uv run ruff check && uv run ruff format --check && uv run pyright` → all pass.
- [ ] **Step 5: CI.** `.github/workflows/ci.yml` runs on PRs and on pushes to main. It uses `astral-sh/setup-uv`, `working-directory: listener`, installs `libportaudio2 libsndfile1` via apt, then runs the four commands from Step 4. ADR 0001: "We record decisions as ADRs in docs/adr, append-only, numbered."
- [ ] **Step 6: Commit** `chore: scaffold listener package, tooling and CI`

---

### Task 2: Config and rule presets

**Files:**
- Create: `listener/src/chase/config.py`, `config/presets/san-antonio.yaml`, `config/chase.example.yaml`, `listener/tests/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class RuleConfig: name: str; confidence_threshold: float; episode_min_minutes: float; max_gap_seconds: float; min_separation_minutes: float; episodes_per_window: int; violation_window_hours: float; citation: str; caveat: str; addressee: str; min_loudness_dbfs: float | None = None` (optional loudness gate from spec §4.1; off by default)
  - `@dataclass(frozen=True) class Settings: rules: RuleConfig; audio_device: str | int | None; capture_rate: int; window_seconds: float; hop_seconds: float; pre_roll_seconds: float; post_roll_seconds: float; spool_minutes: float; backlog_days: int; timezone: str; data_dir: Path; model_path: Path; class_map_path: Path; metrics_port: int; device_label: str`
  - `def load_settings(path: Path, presets_dir: Path) -> Settings`
  - `def load_preset(path: Path) -> RuleConfig`
  - `class ConfigError(ValueError)`

`chase.yaml` names a preset (`rules: {preset: san-antonio}`) and may override any rule key (`rules: {preset: san-antonio, max_gap_seconds: 45}`). Settings defaults: `capture_rate 48000, window_seconds 0.96, hop_seconds 0.48, pre_roll_seconds 30, post_roll_seconds 30, spool_minutes 120, backlog_days 90, timezone "America/Chicago", data_dir "/data", model_path "/models/yamnet.tflite", class_map_path "/models/yamnet_class_map.csv", metrics_port 9100, device_label "USB microphone"`.

`san-antonio.yaml` contains the defaults from Global Constraints plus `addressee: "San Antonio Animal Care Services / 311"`, a `citation` quoting the 2017 draft Chapter 5 text ("a minimum of two separate recorded noise events over a period of twenty-four (24) hours"), and a `caveat` saying that wording is from a 2017 draft and unverified, and that the 10-min/30-s episode definition follows Los Angeles practice because San Antonio sets no per-event duration.

- [ ] **Step 1: Write failing tests:**
  - `test_loads_san_antonio_preset`: all six thresholds equal the Global Constraints values; `"twenty-four (24) hours" in rc.citation`.
  - `test_override_merges_over_preset`: chase.yaml with `max_gap_seconds: 45` → `settings.rules.max_gap_seconds == 45`, other values from the preset.
  - `test_defaults_applied`: a minimal chase.yaml (`rules: {preset: san-antonio}`) → `settings.timezone == "America/Chicago"`, `hop_seconds == 0.48`.
  - `test_unknown_preset_raises`, `test_unknown_key_raises` (catches typos like `max_gap_second`), `test_invalid_values_raise` (each raises `ConfigError` with the key name in the message): `episodes_per_window: 0`, `confidence_threshold: 1.5`, `hop_seconds: 0`, an invalid timezone `Mars/Base`, and `capture_rate: 44100`, which is not a multiple of 16000.
- [ ] **Step 2: Run** `uv run pytest tests/test_config.py` → FAIL.
- [ ] **Step 3: Implement `config.py`** and write the two YAML files. Validate the timezone with `zoneinfo.ZoneInfo`.
- [ ] **Step 4: Run** the tests → PASS.
- [ ] **Step 5: Commit** `feat(listener): settings and San Antonio rule preset`

---

### Task 3: Rule engine I: episodes and runs

**Files:**
- Create: `listener/src/chase/model.py`, `listener/src/chase/rules.py`, `listener/tests/test_rules_episodes.py`, `listener/tests/conftest.py`, `docs/adr/0002-bark-count-is-burst-count.md`

**Interfaces:**
- Consumes: `RuleConfig` (Task 2).
- Produces (`model.py`):
  - `@dataclass(frozen=True) class BarkWindow: start: datetime; end: datetime; confidence: float; loudness_dbfs: float` (tz-aware UTC)
  - `@dataclass(frozen=True) class Episode: id: str; start: datetime; end: datetime; windows: tuple[BarkWindow, ...]; bark_count: int; barking_seconds: float; longest_gap_seconds: float; mean_loudness_dbfs: float; merged_from: int`
  - `@dataclass(frozen=True) class RunStatus: start: datetime; duration_seconds: float; seconds_until_episode: float`
- Produces (`rules.py`):
  - `def episode_id(start: datetime) -> str` → `"ep-" + start.strftime("%Y%m%dT%H%M%SZ")` (deterministic, so it's restart-safe)
  - `def count_bursts(windows: Sequence[BarkWindow], hop_seconds: float) -> int`
  - `def find_episodes(windows: Sequence[BarkWindow], rules: RuleConfig, hop_seconds: float) -> list[Episode]`
  - `def finalized(episodes: Sequence[Episode], now: datetime, rules: RuleConfig) -> list[Episode]`
  - `def current_run(windows: Sequence[BarkWindow], now: datetime, rules: RuleConfig) -> RunStatus | None`

Semantics (from spec §4.2, Ben's decisions):
- **Run:** maximal sequence of windows (sorted by start) where each `next.start - max(end so far) <= max_gap_seconds`. Overlap gives gap 0, never negative.
- **Qualifying run:** `last.end - first.start >= episode_min_minutes`.
- **Merge:** consecutive qualifying runs separated by `< min_separation_minutes` merge into one Episode spanning both, including every window between them. `merged_from` is the number of runs merged.
- **Burst** (estimated bark): maximal sequence of windows where `next.start - prev.end <= hop_seconds`. `bark_count` is the number of bursts. `barking_seconds` is the union length of the windows. `longest_gap_seconds` is the max gap inside the episode, which is reported even when it comes from a merge.
- **finalized:** an episode is final when `now - episode.end >= max(max_gap_seconds, min_separation_minutes*60)`. Before that it could still grow or merge.
- **current_run:** the latest run if `now - last.end <= max_gap_seconds`, else None. `duration_seconds = last.end - first.start`; `seconds_until_episode = max(0, episode_min*60 - duration)`.

`conftest.py` provides `make_windows(start: datetime, seconds: float, hop: float = 0.48, gap_every: float | None = None, gap_len: float = 0) -> list[BarkWindow]`, which builds synthetic windows (0.96 s long, `confidence 0.9`, `loudness -20.0`).

- [ ] **Step 1: Write failing tests** (all thresholds from the SA preset fixture):
  - `test_continuous_11_minutes_is_one_episode` → 1 episode; `id == "ep-20261108T120000Z"` for start 2026-11-08 12:00 UTC.
  - `test_9_minutes_is_not_an_episode` → `[]`.
  - `test_gap_29s_keeps_run_31s_breaks_it`: 6 min + 29 s gap + 6 min → 1 episode; 6 min + 31 s gap + 6 min → `[]`.
  - `test_two_episodes_9min_apart_merge` → 1 episode, `merged_from == 2`, `longest_gap_seconds ≈ 540`.
  - `test_two_episodes_11min_apart_stay_separate` → 2 episodes.
  - `test_unsorted_and_overlapping_windows` (Review Focus 5): shuffled and duplicated windows give the same result as sorted ones; no negative gaps.
  - `test_bursts`: windows at hop 0.48 for 3 s, then a 2 s pause, then 3 s → `count_bursts == 2`.
  - `test_finalized_waits_for_separation`: episode ending at T. At `now = T + 9 min` it is not final; at `T + 10 min` it is final.
  - `test_current_run`: 7m42s of barking, `now` 5 s after the last window → `duration ≈ 462`, `seconds_until_episode ≈ 138`. With `now` 31 s after → None.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `model.py` and the `rules.py` functions above. Pure functions with no clock reads.
- [ ] **Step 4: Run** → PASS. Coverage for `rules.py` ≥ 95% (`uv run pytest --cov=chase.rules`).
- [ ] **Step 5: ADR 0002:** "Bark count = burst count (contiguous barking windows within one hop). Labelled 'estimated' everywhere; barking seconds is the primary measure."
- [ ] **Step 6: Commit** `feat(listener): pure rule engine for runs and episodes`

---

### Task 4: Rule engine II: violations

**Files:**
- Modify: `listener/src/chase/model.py`, `listener/src/chase/rules.py`
- Create: `listener/tests/test_rules_violations.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Violation: id: str; window_start: datetime; window_end: datetime; episode_ids: tuple[str, ...]; revision: int`
  - `def violation_id(first_episode_start: datetime) -> str` → `"v-" + %Y%m%dT%H%M%SZ`
  - `def assign_violations(existing: Sequence[Violation], episodes: Sequence[Episode], rules: RuleConfig) -> list[Violation]`. It returns the full updated list: existing violations, possibly with higher revisions, plus new ones. It's pure and idempotent: calling it again with its own output and the same episodes returns an equal list.

Semantics (spec §4.3, my call as recorded there):
- An episode belongs to at most one violation. Episodes already listed in `existing` are skipped.
- An unassigned episode whose `start` is in `[v.window_start, v.window_end)` of an existing violation is appended to it, with `revision += 1` per appended episode.
- Otherwise it goes into a pending pool, in start order. Pool entries whose start is `>= violation_window_hours` before the newest entry are dropped and stay unassigned. When the pool reaches `episodes_per_window` entries, a new Violation is created: `window_start = first.start`, `window_end = first.start + W`, `revision = 1`, and the pool empties.

- [ ] **Step 1: Write failing tests** (times are hours from T0):
  - `test_two_within_24h_make_violation`: A@0, B@10 → one violation `(A,B)`, rev 1, `id == violation_id(A.start)`.
  - `test_third_inside_window_bumps_revision`: A@0, B@10, C@20 → `(A,B,C)`, rev 2.
  - `test_episode_after_window_starts_fresh`: A@0, B@10, C@30 → violation `(A,B)` only. Then add D@40 → a new violation `(C,D)`.
  - `test_far_apart_never_violate`: A@0, B@30 → `[]`.
  - `test_idempotent`: `assign_violations(out, eps) == out`.
  - `test_episode_never_in_two_violations`: random episode sets (seeded loop of 200 cases) → the union of `episode_ids` has no duplicates.
  - `test_threshold_of_three`: with `episodes_per_window=3`, A@0, B@1 → `[]`; adding C@2 → `(A,B,C)`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(listener): violation assignment with append-only revisions`

---

### Task 5: Bark log store and baselines

**Files:**
- Create: `listener/src/chase/store.py`, `listener/src/chase/baselines.py`, `listener/tests/test_store.py`, `listener/tests/test_baselines.py`

**Interfaces:**
- Consumes: `BarkWindow`, `Episode`, `Violation` (Tasks 3–4).
- Produces (`store.py`), `class BarkLog`:
  - `def __init__(self, path: Path)` creates the schema if missing (tables `windows(start_ts REAL PK, end_ts, confidence, loudness, burst_start INTEGER)`, `episodes(id PK, start_ts, end_ts, json)`, `violations(id, revision, json, PRIMARY KEY(id, revision))`). It uses WAL mode.
  - `add_window(self, w: BarkWindow, burst_start: bool) -> None` (an existing start_ts is ignored, so the call is idempotent)
  - `windows_between(self, start: datetime, end: datetime) -> list[BarkWindow]`
  - `last_window_end(self) -> datetime | None`
  - `save_episode(self, e: Episode) -> bool` (returns False if the id already exists)
  - `episodes_between(self, start: datetime, end: datetime) -> list[Episode]`
  - `latest_violations(self, since: datetime) -> list[Violation]` (highest revision per id with `window_end >= since`)
  - `save_violation(self, v: Violation) -> bool` (returns False if `(id, revision)` already exists)
  - `burst_starts_between(self, start: datetime, end: datetime) -> list[datetime]`
- Produces (`baselines.py`):
  - `@dataclass(frozen=True) class Baselines: barks_this_hour: int; typical_this_hour: float; barks_today: int; daily_average: float; heatmap: dict[tuple[int, int], float]` (key is `(weekday 0=Mon, hour)`, value is average barks)
  - `def compute_baselines(burst_starts: Sequence[datetime], now: datetime, tz: ZoneInfo, days: int = 30) -> Baselines`. It's pure: the caller passes `log.burst_starts_between(now - 31d, now)`. Averages cover the `days` complete local days before today; "today" and "this hour" are local.
  - `def hourly_history(burst_starts: Sequence[datetime], now: datetime, tz: ZoneInfo, days: int = 30) -> list[dict]` → `[{"hour_start": <iso Z>, "barks": int}]` for every local hour in the preceding `days` (zero-filled). This feeds the violation manifest (Task 8) and the report's "barks by hour" chart.

- [ ] **Step 1: Write failing tests:**
  - Store: round-trip windows, episodes, and violations through a `tmp_path` DB. Duplicate `add_window`/`save_episode`/`save_violation` don't create a second row and return False. `latest_violations` returns rev 2 when revs 1 and 2 exist.
  - `test_typical_this_hour`: 30 prior days with 10 bursts each at 07:xx local, `now` = 07:30 local → `typical_this_hour == 10.0`.
  - `test_today_bucket_is_local`: a burst at 05:30 UTC on Nov 9 counts toward local Nov 8 (23:30 CST).
  - `test_dst_fall_back` (Review Focus 3): on 2026-11-01 (fall back), bursts at both 01:30 CDT and 01:30 CST both count toward hour 1 of Nov 1, the day total is right, and nothing raises.
  - `test_heatmap_keys`: 168 keys; a weekday/hour with no data → 0.0.
  - `test_hourly_history_zero_filled`: 30 days → 720 entries (721 across fall-back), with the correct counts where bursts exist.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(listener): SQLite bark log and 30-day baselines`

---

### Task 6: YAMNet detector and loudness

**Files:**
- Create: `listener/src/chase/detector.py`, `listener/tests/test_detector.py`

**Interfaces:**
- Produces:
  - `DOG_CLASSES = frozenset({"Dog", "Bark", "Yip", "Howl", "Bow-wow", "Growling"})` (matched exactly against `display_name` in the class map)
  - `def load_dog_indices(class_map_path: Path) -> dict[int, str]` (raises `ValueError` if none of the names are found)
  - `def to_16k(audio: np.ndarray, from_rate: int) -> np.ndarray` (`scipy.signal.resample_poly`, float32)
  - `def loudness_dbfs(audio: np.ndarray) -> float` (RMS in dBFS; silence → `-120.0` floor)
  - `class BarkDetector: __init__(self, model_path: Path, class_map_path: Path, interpreter_factory: Callable[[str], Any] | None = None)`; `score(self, audio_16k: np.ndarray) -> float`, which returns the max over dog classes of the patch-averaged YAMNet scores. The interpreter is lazy-loaded on first `score` via `ai_edge_litert.interpreter.Interpreter` unless a factory is injected.

- [ ] **Step 1: Write failing tests** (fake interpreter injected; class-map CSV fixture with `Dog` at 69, `Bark` at 70, plus `Speech` at 0):
  - `test_dog_indices_exact_match` (`"Hot dog"` in the CSV is not matched).
  - `test_score_is_max_over_dog_classes`: the fake returns patches whose mean gives Bark 0.8, Dog 0.3, Speech 0.99 → `0.8`.
  - `test_to_16k_length`: 48000 samples at 48 kHz → 16000 samples; float32 dtype.
  - `test_loudness`: a full-scale sine → about -3.0 dBFS (±0.1); zeros → -120.0.
  - `@pytest.mark.integration test_real_model_scores_silence_low`: skipped unless `/models/yamnet.tflite` or `$CHASE_MODEL` exists; 1 s of silence → score < 0.2.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(listener): YAMNet bark detector and loudness`

---

### Task 7: Audio capture, spool, and clip assembly

**Files:**
- Create: `listener/src/chase/audio.py`, `listener/tests/test_audio.py`

**Interfaces:**
- Produces:
  - `class Capture: __init__(self, rate: int, device: str | int | None)`; `start()`; `read(self, timeout: float = 2.0) -> np.ndarray | None` (None on timeout); `stop()`. Mono float32 via `sounddevice.InputStream` (carry over the pattern from birdwatcher `audio.py`).
  - `class Windower: __init__(self, rate: int, window_seconds: float, hop_seconds: float, t0: datetime)`; `push(self, block: np.ndarray) -> list[tuple[datetime, datetime, np.ndarray]]`. Window timestamps derive from the cumulative sample count from `t0`, not the wall clock, so they don't drift.
  - `class Spool: __init__(self, dir: Path, rate: int, segment_seconds: float = 60.0)`; `write(self, t: datetime, block: np.ndarray) -> None` (appends to the current segment and rolls to a new FLAC file named `<utc-start>.flac` every `segment_seconds`); `prune(self, keep_after: datetime) -> int` (deletes segments that end before `keep_after`; returns the count); `extract(self, start: datetime, end: datetime) -> np.ndarray` (stitches segments, sample-accurate, zero-fills any missing span and logs a warning).
  - `def spool_keep_after(now: datetime, open_run_start: datetime | None, spool_minutes: float, pre_roll_seconds: float) -> datetime` → `min(now - spool_minutes, open_run_start - pre_roll)` when a run is open, else `now - spool_minutes`.

- [ ] **Step 1: Write failing tests** (synthetic sine blocks, `tmp_path`; Capture itself isn't unit-tested beyond a monkeypatched `sounddevice` smoke test):
  - `test_windower_timestamps`: at 16 kHz, window 0.96 s, hop 0.48 s, 3 s pushed in 0.1 s blocks → 5 windows; starts at `t0 + 0, 0.48, 0.96, …`; each window has exactly 15360 samples.
  - `test_spool_extract_across_segments`: write 150 s of a ramp signal with `segment_seconds=60`, extract `[t0+50, t0+130]` → `len == 80*rate`, and the samples equal the original slice.
  - `test_spool_prune`: segments ending before `keep_after` are deleted; others are kept.
  - `test_keep_after_protects_long_run` (Review Focus 2): `now = T+100min`, open run started at `T`, `spool_minutes=60`, pre-roll 30 s → `keep_after == T - 30s`.
  - `test_extract_missing_span_zero_fills`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(listener): audio capture, windowing and disk spool`

---

### Task 8: Evidence outbox and v2 backlog clips

**Files:**
- Create: `listener/src/chase/evidence.py`, `listener/tests/test_evidence.py`

**Interfaces:**
- Consumes: `Episode`, `Violation`, `RuleConfig`, `Spool.extract`.
- Produces:
  - `SCHEMA_VERSION = 1`
  - `def write_episode(outbox: Path, episode: Episode, audio: np.ndarray, rate: int, clip_start: datetime, device_label: str, rules: RuleConfig, model_name: str) -> Path`. It writes `outbox/episodes/<id>/audio.flac` (PCM_16) and `metadata.json`. Metadata keys: `schema_version, episode_id, start, end, clip_start, clip_end, barking_seconds, bark_count_estimated, longest_gap_seconds, mean_loudness_dbfs, merged_from, windows: [{start, end, confidence, loudness_dbfs}], device, detector: {model, confidence_threshold}, audio: {file, format, sample_rate, duration_seconds, sha256}`. It writes to `<id>.tmp/` and then renames, so a crash never leaves a half-written directory. It returns the directory.
  - `def write_violation(outbox: Path, violation: Violation, rules: RuleConfig, hourly_history: list[dict]) -> Path`. It writes `outbox/violations/<id>/rev-<n>.json` with keys `schema_version, violation_id, revision, window_start, window_end, episode_ids, rule: {name, thresholds…, citation, caveat, addressee}, hourly_history: [{hour_start, barks}]` (the preceding 30 days, local hours), written atomically.
  - `def write_backlog_clip(backlog_dir: Path, start: datetime, audio: np.ndarray, rate: int, confidence: float, loudness_dbfs: float) -> Path` writes a FLAC file plus a `.json` sidecar. (Embeddings are deferred to v2; the clip is enough to recompute them.)
  - `def prune_backlog(backlog_dir: Path, now: datetime, days: int) -> int`
  - All timestamps in JSON are ISO-8601 UTC with a `Z` suffix.

- [ ] **Step 1: Write failing tests:**
  - `test_episode_files_and_hash`: the metadata `sha256` equals `hashlib.sha256(audio.flac bytes)`; `duration_seconds` matches the samples; no `.tmp` directory remains.
  - `test_episode_rewrite_is_noop`: writing the same episode twice leaves a single directory with identical bytes.
  - `test_violation_revision_files`: rev 1 and rev 2 sit side by side, and `rule.citation` comes from the preset.
  - `test_backlog_prune_90_days`: a clip dated 91 days ago is deleted; one from 89 days ago is kept.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(listener): evidence outbox and local backlog clips`

---

### Task 9: Metrics and events

**Files:**
- Create: `listener/src/chase/metrics.py`, `listener/src/chase/events.py`, `listener/tests/test_metrics.py`, `listener/tests/test_events.py`

**Interfaces:**
- Produces (`metrics.py`): `class Metrics: __init__(self, registry: CollectorRegistry | None = None)`, using a private registry by default; `serve(self, port: int) -> None`. Attributes. **These names are pinned so the dashboard in Task 12 matches:**
  - Counters: `chase_barking_windows_total`, `chase_barks_total` (estimated), `chase_episodes_total`, `chase_violations_total`, `chase_audio_errors_total{reason}` (reasons: `capture`, `inference`, `spool`, `evidence`)
  - Gauges: `chase_current_run_seconds`, `chase_seconds_until_episode`, `chase_barking_now` (0/1), `chase_in_episode` (0/1), `chase_episodes_in_window`, `chase_episodes_per_window` (the threshold), `chase_barks_this_hour`, `chase_typical_barks_this_hour`, `chase_barks_today`, `chase_daily_average_barks`, `chase_heatmap_barks{weekday,hour}`, `chase_outbox_pending`, `chase_heartbeat_timestamp_seconds`
  - Histograms: `chase_bark_confidence` (buckets 0.5–1.0 in steps of 0.05), `chase_bark_loudness_dbfs` (buckets -80 to 0 in steps of 5)
  - Method `apply_baselines(self, b: Baselines) -> None`.
- Produces (`events.py`): `def emit(event: str, **fields) -> None` writes one JSON line to stdout: `{"ts": <iso Z>, "event": event, **fields}`. Event names: `episode`, `violation`, `listener_started`, `capture_error`. The `episode` event carries `episode_id, start, end, duration_seconds, bark_count_estimated, violation_id (or null)`. The `violation` event carries `violation_id, revision, episode_ids`.

- [ ] **Step 1: Write failing tests:**
  - `apply_baselines` sets all five baseline gauges and 168 heatmap series (read back with `registry.get_sample_value`).
  - All pinned metric names are present in `generate_latest(registry)`.
  - `emit` writes valid JSON with `ts` ending in `Z` (via `capsys`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(listener): Prometheus metrics and JSON events`

---

### Task 10: Service loop, restart safety, failure handling

**Files:**
- Create: `listener/src/chase/service.py`, `listener/tests/test_service.py`, `docs/adr/0003-listener-state-is-the-bark-log.md`

**Interfaces:**
- Consumes: everything above.
- Produces: `class Service: __init__(self, settings: Settings, log: BarkLog, detector: BarkDetector, spool: Spool, metrics: Metrics, clock: Callable[[], datetime] = utc_now)`, plus:
  - `def on_window(self, start: datetime, end: datetime, audio_16k: np.ndarray, audio_native: np.ndarray) -> None` scores the window. If `score >= threshold` and (`min_loudness_dbfs` is None or loudness ≥ it), it computes loudness, decides `burst_start`, calls `log.add_window`, updates the counters and histograms, and writes a backlog clip.
  - `def tick(self) -> None` runs every 10 s. It recomputes rule state from the log: windows since `max(now - (violation_window_hours + 2h), last finalized episode end)`, then `find_episodes` and `finalized`. Each finalized episode not yet saved is extracted from the spool (pre/post-roll), written with `write_episode`, saved with `log.save_episode`, and announced with an `episode` event. Then `assign_violations(log.latest_violations(now - 48h), saved episodes from the last 48 h)`. Each `(id, revision)` not yet saved is written with `write_violation`, saved, and announced with a `violation` event. The tick also updates the gauges (`current_run`, `in_episode`, episodes in window, `outbox_pending`), prunes the spool via `spool_keep_after`, and recomputes baselines every 10 minutes.
  - `def on_capture_timeout(self) -> None` increments `chase_audio_errors_total{reason="capture"}` and emits `capture_error`. The heartbeat is **only** advanced in `on_window`, so a dead mic stops the heartbeat (Plan 2's alert watches it).
  - `def run(self, capture: Capture) -> None` is the blocking loop: read → `spool.write` → windower → `on_window`, calling `tick` on schedule and `on_capture_timeout` on a None read. After 30 consecutive timeouts it stops and restarts the capture stream instead of exiting.

ADR 0003: "All listener state is derived from the SQLite bark log. Deterministic episode/violation IDs + `save_*` returning False on duplicates make every tick idempotent, so a restart at any point loses nothing and duplicates nothing. The cost is that evidence for an episode is written up to `min_separation_minutes` after barking stops."

- [ ] **Step 1: Write failing tests** (fake detector returning scripted scores, `tmp_path` log/spool/outbox, controllable clock; feed windows directly to `on_window` with synthetic audio also written to the spool):
  - `test_episode_written_once_after_separation`: 11 min of barking → no outbox entry at `end + 9 min`; at `end + 10 min`, one `episodes/<id>/` and `chase_episodes_total == 1`; a further `tick` changes nothing.
  - `test_two_episodes_make_violation_manifest`: 11 min at 08:00 and 11 min at 20:00 → `violations/v-…/rev-1.json` lists both IDs, and one `violation` event is emitted.
  - `test_restart_mid_episode` (Review Focus 1): feed 6 min, build a **new** `Service` on the same log/spool/outbox, feed 5 more min, tick past separation → exactly one episode spanning all 11 min and no duplicate files or events.
  - `test_restart_after_violation_no_new_revision`: a new Service after a violation → no `rev-2`.
  - `test_capture_timeout_counts_and_heartbeat_stalls` (Review Focus 4): after `on_capture_timeout` ×3, the error counter is 3 and the heartbeat value is unchanged since the last window.
  - `test_episode_clip_includes_pre_post_roll`: the clip duration equals the episode duration + 60 s (±1 window).
  - `test_loudness_gate`: with `min_loudness_dbfs: -30`, a window at -40 dBFS scoring 0.9 is not logged; with the gate unset it is.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. Then the full suite plus ruff and pyright.
- [ ] **Step 5: Commit** `feat(listener): service loop with idempotent episode/violation handling`

---

### Task 11: CLI: `chase run` and `chase replay`

**Files:**
- Create: `listener/src/chase/cli.py`, `listener/tests/test_cli.py`

**Interfaces:**
- Produces: `def main(argv: list[str] | None = None) -> int` (argparse).
  - `chase run --config PATH [--presets DIR]` builds everything from Settings, starts `Metrics.serve`, emits `listener_started`, and calls `Service.run`.
  - `chase replay --config PATH [--preset FILE] [--since YYYY-MM-DD] [--until YYYY-MM-DD]` reads the bark log at `data_dir/chase.sqlite` **read-only**, runs `find_episodes` + `assign_violations(existing=[])` with the given preset (default: the configured one), and prints one line per episode (`start–end (local), duration, est. barks, merged_from`) and per violation (`id, episodes`). It ends with a summary like `N episodes, M violations`. It never writes to the outbox or the log. Exit code 0.

- [ ] **Step 1: Write failing tests:**
  - `test_replay_prints_episodes_and_violations`: seed a log with two 11-min bouts 12 h apart → output contains `2 episodes, 1 violations`, and the outbox dir stays empty.
  - `test_replay_alternate_preset_changes_result`: a preset with `episode_min_minutes: 15` → `0 episodes, 0 violations`.
  - `test_replay_does_not_modify_db`: the db file's mtime and bytes are unchanged.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(listener): run and replay commands`

---

### Task 12: Container, Compose, Alloy, dashboard, docs

**Files:**
- Create: `listener/Dockerfile`, `compose.yaml`, `alloy/config.alloy`, `.env.example`, `grafana/dashboard.json`, `docs/running-on-the-optiplex.md`
- Modify: `README.md` (what it does, architecture diagram from spec §3, quick start, metrics table, "the ordinance caveat", screenshots later)

**Interfaces:**
- Consumes: metric names (Task 9), event names (Task 9), `chase run` (Task 11).

Requirements:
- **Dockerfile:** `python:3.12-slim`; apt `libportaudio2 libsndfile1`; uv sync, frozen, no dev deps. At build time, download the model from `https://storage.googleapis.com/mediapipe-assets/yamnet.tflite` and the class map from `https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv` into `/models/`, verifying both against SHA-256 values pinned in the Dockerfile (record the hashes when first building). Runs as a non-root user in the `audio` group. `ENTRYPOINT ["chase", "run", "--config", "/config/chase.yaml", "--presets", "/config/presets"]`.
- **compose.yaml:**
  - `listener`: `devices: ["/dev/snd:/dev/snd"]`, `group_add: [audio]`; mounts `./config:/config:ro` and the named volume `chase-data:/data`; `restart: unless-stopped`; uses the JSON log driver; healthcheck curls `localhost:9100/metrics`.
  - `alloy`: `grafana/alloy` pinned to a specific version; mounts `./alloy/config.alloy` and `/var/run/docker.sock:ro`; `env_file: .env`; `restart: unless-stopped`.
- **alloy/config.alloy:** scrape `listener:9100` every 15 s and `prometheus.remote_write` to Grafana Cloud using `env("GRAFANA_PROM_URL")`, `env("GRAFANA_PROM_USER")`, and `env("GRAFANA_TOKEN")`. Add `discovery.docker` + `loki.source.docker` for the `listener` container, then `loki.process` with a `stage.json` that extracts `event` as a label, then `loki.write` using `env("GRAFANA_LOKI_URL")`, `env("GRAFANA_LOKI_USER")`, and `env("GRAFANA_TOKEN")`.
- **.env.example:** those five variable names, without values.
- **grafana/dashboard.json:** the approved layout (spec §6), using the Prometheus and Loki datasources as template variables (`${prom}`, `${loki}`) so it imports into any stack:
  - Top row stats: `chase_current_run_seconds` (with `chase_seconds_until_episode` as subtitle text), `chase_barks_this_hour` vs `chase_typical_barks_this_hour`, `(chase_barks_today / chase_daily_average_barks - 1) * 100`, and `chase_episodes_in_window` / `chase_episodes_per_window`.
  - Middle: bars of `increase(chase_barks_total[1h])` over 24 h, with `chase_typical_barks_this_hour` overlaid as a dashed line. Recent-episodes table from `{container="listener", event="episode"} | json`.
  - Bottom: a state timeline of `chase_barking_now` and `chase_in_episode` for today, and a heatmap of `chase_heatmap_barks` (weekday rows × hour columns; instant query, table format, grouping transformation).
  - In this plan the dashboard is imported by hand. Terraform management comes in Plan 2.
- **docs/running-on-the-optiplex.md:** confirm NTP sync (`timedatectl` shows `System clock synchronized: yes`; evidence timestamps depend on it), find the mic with `arecord -l`, set `audio_device`, `cp config/chase.example.yaml config/chase.yaml`, fill in `.env`, run `docker compose up -d`, check `docker compose logs -f listener`, import the dashboard, and run `chase replay` via `docker compose run --rm listener chase replay --config /config/chase.yaml --since …`.

- [ ] **Step 1:** `docker compose config` validates. `docker build -f listener/Dockerfile .` succeeds (model hashes verified).
- [ ] **Step 2:** Smoke-test on a dev machine without a mic: `docker compose run --rm listener chase replay --config /config/chase.yaml` on an empty volume prints `0 episodes, 0 violations`.
- [ ] **Step 3:** `python -c "import json; json.load(open('grafana/dashboard.json'))"` passes, and the dashboard imports into Grafana Cloud. Verifying this needs Ben.
- [ ] **Step 4:** On the OptiPlex (Ben): `docker compose up -d`, play a dog-bark video near the mic → `chase_barking_windows_total` rises in Grafana and backlog clips appear in the volume.
- [ ] **Step 5: Commit** `feat: container, compose stack, Alloy config, Grafana dashboard and docs`

---

## Plan 2 (next, separate document): Cloud evidence pipeline

Outline only; written after Plan 1 lands:
- GCP bootstrap + Terraform: state bucket, project APIs, private bucket with 30-day/365-day lifecycle, Artifact Registry, WIF for GitHub Actions, Secret Manager entries, listener service account + key, with ADR 0004 for the key exception.
- Outbox uploader in the listener: ships `outbox/` to GCS with create-only credentials, retries and backoff, deletes local copies after confirmed upload, drives `chase_outbox_pending`.
- Report service (Cloud Run, Eventarc on `violations/**/rev-*.json`): copies to `evidence/`, encodes MP3, renders the letter + evidence-packet PDF, sends the Gmail SMTP email (with a size fallback to links), and writes signed URLs.
- Alerting: Grafana alert rules (heartbeat absent 15 min, capture errors, outbox backlog) and a Cloud Monitoring alert on report failures. Dashboard and alerts managed through the Terraform Grafana provider.
