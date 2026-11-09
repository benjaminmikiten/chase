# chase — design

> *No job's too big, no pup's too loud.*

`chase` listens to the neighborhood through a USB mic, detects dog barking, puts the barking on a Grafana Cloud dashboard, and when the barking crosses a configurable legal threshold, packages audio evidence and a ready-to-forward complaint so the owner can send it to the city.

It is a public GitHub project, built partly to show off, so it should be well structured, well tested, and documented, and it must never commit anything personal.

---

## 1. Why

Several neighboring households have dogs that bark a lot, including some with a rotating set of dogs, so it isn't one consistent dog. Ben wants to be able to:

1. **See** when and how much the dogs bark (dashboard).
2. **Know** when barking crosses the legal limit (violation detection).
3. **Prove** it to the city with recordings and a professional report, with as little effort as possible on his part.

This replaces an earlier project (`birdwatcher`, a Raspberry Pi bird + bark detector that only exported counts). `chase` starts fresh: it is dog-only, runs on an x86 Dell OptiPlex in Ben's room with Docker Compose, and puts its cloud side in Google Cloud using the same patterns as Ben's `tower` project.

## 2. Goals, non-goals, anti-goals

### Goals (v1)

- Continuously listen on a USB mic and classify short audio windows as barking or not.
- Emit bark metrics and episode events to **Grafana Cloud** and ship a dashboard (§6).
- Detect **episodes** and **violations** with a configurable rule (§4). Ship San Antonio, TX as the default preset.
- For each episode, keep the audio. On a violation, keep the evidence long-term, generate a **PDF report** (§7), and **email it to Ben** so it is ready to forward to 311 or the city council.
- Infrastructure as code. Cloud costs nothing at idle (scale to zero).

### Non-goals (v1)

- **Telling dogs apart.** v1 counts every dog in earshot. The report wording reflects this (§7.3). A "which dog" classifier is v2 (§11).
- **Direction finding** (two mics, time difference). Ruled out entirely, not deferred.
- **Sending anything to the city automatically.** Ben always reviews and forwards by hand.
- **Real Gmail drafts** (Gmail API). Email-to-self is enough. Maybe later.
- **Bird detection.** Dropped.
- A web UI. Grafana is the UI. The v2 labeling screen is the first web UI.

### Anti-goals (would make this a failure even if it "works")

- **Personal data in the public repo.** No home address, neighbor addresses, coordinates, email addresses, GCP project ID, bucket names, recordings, or reports. Not in code, config, fixtures, screenshots, or git history.
- **Evidence that doesn't hold up.** Wrong timestamps, clips that start mid-episode, audio that looks edited, or a report claiming a specific dog or address made noise when v1 can't know that.
- **Recording the neighborhood indiscriminately.** Audio leaves the OptiPlex only when it's part of an episode. Rolling audio is overwritten locally.
- **A fixed monthly cloud bill.** No always-on instances, load balancers, NAT, Cloud SQL, etc. Ask before adding anything with fixed monthly cost.

## 3. Architecture

```
 OptiPlex (Linux, Docker Compose, always on)                 Google Cloud (own project, scale to zero)
 ┌────────────────────────────────────────────┐              ┌──────────────────────────────────────────┐
 │ listener (Python)                          │              │ GCS bucket (private)                     │
 │  USB mic → windows → YAMNet bark score     │  upload      │  episodes/…   (30-day lifecycle)          │
 │  → bark events → episode/violation logic   │ ───────────▶ │  evidence/…   (365-day lifecycle)         │
 │  rolling audio ring buffer (local disk)    │  FLAC+JSON   │  reports/…                                │
 │  SQLite log of barks/episodes              │              │        │ object-finalize (Eventarc)        │
 │  /metrics (Prometheus) + JSON event logs   │              │        ▼                                   │
 ├────────────────────────────────────────────┤              │ Cloud Run "report" service (Python)       │
 │ alloy (Grafana Alloy)                      │              │  copies episodes → evidence/              │
 │  scrape /metrics → remote_write            │              │  renders MP3, PDF (letter + packet)       │
 │  docker logs → Loki                        │              │  emails Ben via Gmail SMTP                │
 └───────────────┬────────────────────────────┘              │ Secret Manager · Artifact Registry        │
                 ▼                                           └──────────────────────────────────────────┘
          Grafana Cloud (Prometheus + Loki + dashboard)       GitHub Actions → GCP via Workload Identity Federation
```

### 3.1 On the OptiPlex (Docker Compose)

- **`listener`** container: captures audio from the USB mic (ALSA device passed into the container), runs bark detection, keeps state, and uploads episode audio.
- **`alloy`** container: Grafana Alloy scrapes the listener's `/metrics` and forwards the listener's structured JSON logs to Grafana Cloud Loki.
- Both are in `compose.yaml` in the repo. Credentials live in a git-ignored `.env` and a mounted service-account key file (§8.3).

### 3.2 In Google Cloud

- **Its own GCP project** (not tower's), set up the way tower is (§8).
- A private **GCS bucket** for audio, metadata, and reports.
- A **Cloud Run service** (`min_instances = 0`) triggered by GCS object-finalize events through Eventarc when a violation manifest lands. It assembles evidence, renders the report, and sends the email.

**Why GCS upload + event instead of the listener calling Cloud Run directly:** episode audio can be tens of MB, the upload needs to survive the handler being cold or failing, and the OptiPlex's credential can then be limited to "create objects in one bucket" (§8.3).

### 3.3 Repo layout (suggested; builder may adjust)

```
listener/          Python package + Dockerfile (runs on the OptiPlex)
report/            Python package + Dockerfile (Cloud Run)
shared/            shared types: episode/violation manifest schema, rule config (if it earns its keep)
compose.yaml       OptiPlex stack (listener + alloy)
alloy/             Alloy config
grafana/           dashboard JSON
infra/             Terraform: envs/prod, bootstrap.sh, README
config/            example config + rule presets (san-antonio.yaml); real config is git-ignored
docs/adr/          decision records
docs/superpowers/  this design + plans
```

## 4. Detection rules

### 4.1 Bark detection

- Audio is captured continuously. Every **N seconds** (configurable window, default about 1 s with overlap; builder's call), the window is scored by **YAMNet** for its dog/bark classes ("Bark", "Dog", "Bow-wow", "Yip", "Growling", "Howl"; builder confirms the class list from the YAMNet class map).
- A window scoring at or above `confidence_threshold` (default **0.7**, configurable) is a **barking window**.
- Loudness (dBFS, RMS or peak) is computed and stored for every barking window. v1 doesn't gate on it by default, but an optional `min_loudness_dbfs` config exists (default off). Loudness is stored because v2 and future tuning will want it.
- **"Barks" (the count) vs. "barking time":** YAMNet scores frames, not individual barks. v1 reports **barking seconds** as the primary measure and an **estimated bark count** (contiguous barking frames merged into one bark, or onset detection within barking windows; builder's call, documented in an ADR). Dashboard and report label the count as estimated.

The YAMNet code in `birdwatcher` (`birdwatcher/detectors/dogbark.py`, `birdwatcher/audio.py`) can be carried over as a starting point. Note it assumed an ARM `tflite-runtime`; on x86 use a current TFLite / LiteRT runtime.

### 4.2 Episodes

An **episode** is a stretch of barking that:

- lasts at least `episode_min_minutes` (default **10**), and
- has no gap between barking windows longer than `max_gap_seconds` (default **30**).

Barking that stops (gap greater than `max_gap_seconds`) before reaching the minimum is just barking: it's logged and counted, but it's not an episode.

**Separation (Ben's decision):** two qualifying episodes that are less than `min_separation_minutes` apart (default **10**) are **merged into one episode**. The report shows the gap inside it. This keeps one long bout with a short pause from counting as two "separate" events. The merged episode's audio covers the whole span.

**Tunable by design (Ben's decision):** the thresholds will be adjusted once real data comes in, so the rule engine must not be brittle:
- All thresholds live in config/presets; none are hard-coded.
- The rule engine is a pure function over the stored bark log (barking windows with timestamps, confidence, loudness). It does no audio work and holds no hidden state, and is unit-tested heavily.
- A **replay command** (`chase replay --preset <file> --since <date>`) re-runs the rules over the SQLite bark log and prints the episodes and violations they would have produced, so Ben can try new thresholds against real history before changing them live. Replay never uploads or emails.

The listener exposes the **current run** (barking in progress, gap not yet exceeded) so the dashboard can show "barking right now: 7m 42s, 2m 18s until it counts."

### 4.3 Violations

A **violation** occurs when there are at least `episodes_per_window` (default **2**) episodes within a rolling `violation_window_hours` (default **24**) window.

- Basis: a 2017 City of San Antonio draft of Code Chapter 5 defines an animal noise nuisance as "a minimum of two separate recorded noise events over a period of twenty-four (24) hours." San Antonio does not set a per-event minute threshold; 10 min with gaps under 30 s follows Los Angeles. **The current ordinance wording is unverified** (the 2017 PDF is gone, and Municode blocked automated reads). The README and preset file must say so, and Ben will confirm with 311.
- A violation triggers **one report** as soon as the qualifying episode ends.
- **Windowing (my call):** each episode belongs to **at most one** violation. A violation's window runs `violation_window_hours` from the start of its **first** episode. Later episodes that start inside that window are added to it and trigger an **updated report** that includes every episode so far; the email subject says "updated." Once the window closes, counting starts fresh from episodes not already used. (Example: A at 0 h and B at 10 h are a violation, with a window of 0–24 h. C at 30 h starts a new count; it is not paired with B.)
- **Manifests are append-only** (the listener's credential is create-only): each update is a new object, `violations/<violation-id>/rev-<n>.json`. The report service uses the highest revision.
- Rolling vs. calendar day: rolling, matching the ordinance language. The report's headline gives the actual date and time range covered.

### 4.4 Rule presets

Rules are config, not code: `config/presets/san-antonio.yaml` ships as the default, and other cities can be added as presets. A preset carries its thresholds and the ordinance citation and caveat text that the report prints.

## 5. Audio handling

- **Rolling ring buffer** on the OptiPlex's local disk (or memory), long enough to cover the longest gap plus pre-roll. When an episode ends, the listener assembles its audio from **30 s before the first bark to 30 s after the last**.
- Audio leaves the machine only as part of an episode. Everything else is overwritten.
- When an episode ends, the listener writes the clip as **FLAC (lossless master)** together with a JSON metadata file (start and end timestamps, barking windows with confidences and loudness, gaps, device info, detector model and threshold, SHA-256 of the FLAC) and uploads both to `episodes/<id>/`. Upload retries with backoff and queues locally if offline.
- When a violation triggers, the listener uploads a **violation manifest** (`violations/<violation-id>/rev-<n>.json`, listing the episode IDs, the rule used, and the hourly history; see §4.3). Its creation is the Eventarc trigger.
- **Retention:**
  - `episodes/` is kept 30 days (GCS lifecycle). That's long enough that an episode can later become part of a violation and be reviewed.
  - On a violation, the report service copies that violation's episodes to `evidence/<violation-id>/`, which is kept 365 days.
  - Reports go under `evidence/<violation-id>/` as well.
  - Local rolling audio is minutes, not days.
- **v2 training backlog (Ben's decision):** the listener also saves short clips of **every barking window** (a few seconds each, with the YAMNet embedding and metadata) to the OptiPlex's local disk, **never uploaded**, deleted after **90 days** (configurable). This builds a backlog to label once v2 starts. These clips are covered by the same `.gitignore` and privacy rules.
- **Clock:** the OptiPlex must be NTP-synced. Timestamps are stored in UTC and rendered in `America/Chicago` (configurable).

## 6. Dashboard (Grafana Cloud)

Agreed layout (mocked up and approved):

**Top row (stat panels)**
- **Barking right now:** duration of the current run, plus time remaining until it counts as an episode.
- **Barks this hour:** compared with the typical count for this hour of day.
- **Today vs. 30-day average:** percent above or below, with raw numbers.
- **Episodes in the current window:** e.g. "2 / 2", with violation status.

**Middle**
- **Barks per hour, last 24 h** (bars), with a dashed line for the 30-day average for that hour.
- **Recent episodes** table: when, length, whether a report was triggered. Source: Loki episode events.

**Bottom**
- **Barking timeline for today:** barking windows lightly shaded, episodes darker.
- **Heatmap** of barking by hour × weekday over the last 30 days.

Metrics are Prometheus (counters for barking windows and estimated barks, a gauge for current-run seconds, histograms for confidence and loudness, error counters). Episode, violation, and upload events are structured JSON log lines sent to Loki. Exact metric names are the builder's call.

**30-day baselines come from the listener, not Grafana (my call).** Grafana Cloud's free tier keeps roughly 14 days of metrics and logs (verify against current pricing), which isn't enough for the 30-day panels. The listener computes these from its SQLite bark log and exports them as gauges: the average for each hour of the day, the daily average, and an hour × weekday matrix (168 labelled series) for the heatmap. This works on any plan and needs no paid tier. The "recent episodes" table covers what Loki retains; that's acceptable.

Assume Ben's **existing** Grafana Cloud stack. Terraform manages the dashboard, alert rules, and access tokens, not the stack itself.

The "report triggered" column means the violation manifest was uploaded (the listener knows this). Whether the email went out is visible in Cloud Logging and covered by alerting (§6.1).

### 6.1 Alerting (my call)

A monitor that silently stops collecting evidence defeats the point, so v1 includes minimal alerting, all free:
- **Grafana alert → email Ben** when the listener's heartbeat metric is absent for 15 minutes (container down, mic gone, Alloy broken), when audio-capture errors spike, or when the upload queue backs up.
- **Google Cloud Monitoring alert → email Ben** when the report service logs a failed run or fails to send an email (log-based metric).

The dashboard JSON lives in `grafana/` and is applied with Terraform's Grafana provider (my call; manual import is an acceptable fallback if the provider proves painful).

## 7. The report and email

### 7.1 PDF: letter + evidence packet (approved)

**Page 1, plain complaint letter**, professional and readable by anyone:
- Title, report date, reference ID.
- Addressee: San Antonio Animal Care Services / 311 (from preset).
- One paragraph: on [date range], N separate episodes of sustained barking were recorded within 24 hours.
- Table: episode #, start, end, duration, estimated barks.
- Note that audio recordings are attached and linked.
- Sender block: name, address, phone (from private config).

**Following pages, evidence packet:**
- Headline with the rule applied (thresholds, ordinance citation, and caveat from the preset).
- Detailed table: start and end to the second, duration, estimated barks, longest gap, mean loudness.
- A **timeline strip** per episode (barking vs. quiet).
- **Barks by hour** for the day, compared with the 30-day average for each hour. (The report service needs this history: the listener includes hourly counts for the preceding 30 days in the violation manifest. My call.)
- **Recording details:** each file's name, duration, format, SHA-256 (both FLAC and MP3); device and fixed location; detector model and threshold; clock source and timezone.

PDF rendering library is the builder's call (HTML→PDF with WeasyPrint is a natural fit for the mockup's look).

### 7.2 Email

- Sent **from Ben's Gmail to Ben's Gmail** over SMTP with an app password (stored in Secret Manager).
- **The email is the complaint:** the subject is like "Barking dog complaint: Nov 8, 2026 (2 episodes)" and the body is the page-1 letter text, so Ben can hit Forward, add the 311 or council address, and send.
- Attachments: the PDF, plus each episode as **MP3** (mono, about 64 kbps). If the attachments would exceed about 20 MB, attach only the PDF and include links to the audio instead.
- Links to the FLAC masters: GCS signed URLs (V4, max 7 days). The email says they expire. Durable sharing can come later.

### 7.3 Honest wording (v1)

v1 can't attribute barking to a specific dog or house. The letter says "sustained dog barking from neighboring properties, including [configured addresses]" and never claims one address is the sole source. When the v2 classifier exists, the wording can become specific.

## 8. Infrastructure (copied from `tower`'s patterns)

### 8.1 What carries over from tower

- **Everything is Terraform** (`infra/envs/prod`), with state in a versioned GCS bucket provided at `terraform init`. A guided **`infra/bootstrap.sh`** covers the one-time manual steps (create project, link billing, state bucket, `gcloud auth application-default login`), documented in `infra/README.md`.
- **Agents never run `terraform apply`.** They run `plan` and show Ben. Ben applies.
- **Cloud Run, `min_instances = 0`.** Artifact Registry for images.
- **GitHub Actions authenticates to GCP with Workload Identity Federation**, restricted to this repo and the main branch. CI builds and pushes the report image and deploys it.
- **Secret Manager** for secrets (Gmail app password, sender/recipient address, personal letter fields).
- `terraform.tfvars` is git-ignored. Project ID, bucket names, and personal values are never committed. A `terraform.tfvars.example` is committed.
- **Decision records** in `docs/adr/`, **conventional commits**, one branch and PR per ticket.
- **Ask before anything with a fixed monthly cost.**

### 8.2 Stack choices

- **Python** for both the listener and the report service (one language; YAMNet's ecosystem is Python). Tower's TypeScript stack does not carry over.
- Tooling (builder's call, suggested): `uv`, `pytest`, `ruff`, type-checked with `mypy` or `pyright`.
- Testing in tower's spirit: thorough unit tests for the rule engine (episodes, gaps, violation windows, updates), audio assembly (pre/post-roll, boundaries), manifest schema, and report rendering. **Never hit real GCP or Gmail in tests.** Use synthetic audio or a public, licensed bark sample, **never recordings of the actual neighbors**.

### 8.3 OptiPlex credentials (exception to tower's "no keys" rule)

The OptiPlex is not a GitHub Action or a Cloud Run service, so it can't use keyless auth. It gets a **service-account key file** for a dedicated `listener` service account whose only role is **`roles/storage.objectCreator` on the evidence bucket** (scoped to `episodes/` and `violations/` with IAM conditions if practical). It can't read, list, or delete. Record this exception in an ADR. The key is mounted into the container and never committed.

Grafana Cloud credentials for Alloy (remote_write and Loki push tokens) also live in the git-ignored `.env` on the OptiPlex.

## 9. Configuration and privacy

- `config/chase.example.yaml` (committed) and `config/chase.yaml` (git-ignored): audio device, thresholds, preset choice, timezone.
- Personal values (Ben's name, address, phone, email, neighbor addresses) are **never in repo config**. They live in Secret Manager and are read by the report service. Their names are in `terraform.tfvars.example`, without values.
- `.gitignore` covers `.env`, key files, `chase.yaml`, `terraform.tfvars`, any local audio, SQLite DBs, and generated reports from day one.
- README screenshots of the dashboard or report use synthetic or redacted data.

## 10. Decided vs. left to the builder

**Decided with Ben:** OptiPlex + Docker Compose; Grafana Cloud; own standalone GCP project and repo named `chase`; tower's infra patterns; public repo; rule thresholds (10 min / 30 s gaps / 2 per 24 h, San Antonio preset); report = letter + evidence packet; email-to-self that is ready to forward; dashboard layout; v1 counts all dogs; which-dog classifier is v2; direction finding is out.

Also decided with Ben after review: episodes less than 10 min apart merge into one; business logic must be easy to tune (config, pure rule engine, replay command); keep local-only bark clips for 90 days as a v2 training backlog.

**My calls (Claude), flagged for Ben's review:** Python everywhere; GCS upload + Eventarc trigger; 30 s pre/post-roll; FLAC master + MP3 attachment; 30-day / 365-day retention; Gmail SMTP with app password; violation windows anchored on their first episode, each episode used once, with append-only manifest revisions; SQLite local log; Loki for episode events; 30-day baselines computed by the listener and exported as gauges (free-tier-safe); minimal alerting (heartbeat, errors, report failures); dashboard and alerts via Terraform Grafana provider; the listener's key limited to object creation; hourly history shipped in the violation manifest.

**Left to the builder:** window length and overlap; exact YAMNet classes; bark-count estimation method; metric names and PromQL; PDF library; repo internals; dependency choices; how the listener persists state across restarts (an episode in progress during a restart should not be lost or double-counted; document the approach).

## 11. Later phases

- **v2: which dog.** Use YAMNet embeddings plus a small classifier ("target dogs" vs. "other"). A labeling and review screen lets Ben label clips and correct mislabels; retrain from corrections. Some nearby dogs change over time, so the "other" class must stay open-ended. The report wording gets specific once this exists.
- **Maybe:** phone push (ntfy or Pushover) when a run passes 10 minutes; real Gmail drafts addressed to the city; durable share links for evidence.

## 12. Project tracking

- Design and planning live in **Linear** (project: chase).
- Implementation tickets are **GitHub issues** on the public `chase` repo.
