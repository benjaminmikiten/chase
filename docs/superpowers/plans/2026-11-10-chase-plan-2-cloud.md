# chase Plan 2: Cloud Evidence Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evidence the listener writes to its outbox reaches a private GCS bucket. When a violation manifest lands, a scale-to-zero Cloud Run service assembles the evidence, renders the letter + evidence-packet PDF, and emails Ben a ready-to-forward complaint. Everything is Terraform, deployed by keyless CI, with alerts when anything stops.

**Architecture:**
- **Upload:** the listener's new uploader thread pushes `outbox/` to GCS with a create-only key.
- **Trigger:** a GCS notification on `violations/` publishes to Pub/Sub, and a push subscription (OIDC, 600 s ack deadline) calls the `report` Cloud Run service.
- **Report service:** claims the manifest revision with a create-if-absent marker, then:
  - copies the episodes to `evidence/`;
  - encodes MP3s with ffmpeg, one episode at a time;
  - renders HTML → PDF with WeasyPrint;
  - sends the email over Gmail SMTP.

**Tech Stack:**
- **Listener:** Python 3.12, `google-cloud-storage`.
- **Report service:** Flask + gunicorn, Jinja2, WeasyPrint, ffmpeg.
- **Infra:** Terraform 1.5.7 (google provider ~> 6.0; grafana provider optional), GitHub Actions (Workload Identity Federation).

**Spec:** `docs/superpowers/specs/2026-11-09-chase-design.md` (§§3.2, 5, 6.1, 7, 8, 9). Plan 1 is merged.

## Global Constraints

- **Public repo.** No personal data in code, fixtures, rendered samples or history. Fixtures use invented names ("Pat Example", "123 Sample St").
- **Merging deploys the listener** (the OptiPlex cron follows `main`). With no `gcs_bucket` set and no `secrets/` present, the listener must behave exactly as it does today. Upload problems are logged and counted (`audio_errors{reason="upload"}`) and never crash it.
- **Agents never run `terraform apply` or `plan` against real infrastructure, never set GitHub variables or secrets, and never create keys.** Only `fmt`, `validate`, and `init -backend=false`.
- **No service-account keys in Terraform state.** The listener key is created by hand with `gcloud` in `infra/bootstrap.sh` (ADR 0004).
- **Scale to zero, no fixed monthly cost.** Cloud Run `min_instance_count = 0`, with no load balancer, NAT or always-on resources.
- **Report wording (spec §7, Ben's decisions):**
  - barking **minutes** is the headline;
  - the count is always "barking bursts (estimated)", never "barks";
  - no single address is named as the source ("neighboring properties, including …");
  - print the preset's citation **and** caveat;
  - disclose `audio.complete` and any gaps;
  - list SHA-256 for FLAC **and** MP3;
  - all times in local time (`America/Chicago`, from the manifest/config).
- **At-least-once delivery.** Exactly one email per manifest revision: claim with `if_generation_match=0` **before** sending. Run with max instances 1 and concurrency 1.
- **Bounded memory.** Cloud Run `/tmp` is RAM. Process one episode at a time, stream through files, and delete temp files as you go.
- **Email size.** Attachment budget is measured on **base64-encoded** size, `attachment_limit_mb` (default **15** encoded MB). Over budget means the PDF is attached and the audio is sent as links.
- **Tests never touch** the network, real GCS, real SMTP or real GCP. WeasyPrint and ffmpeg tests run in the report Docker image and in CI.

## Review Focus

1. **Duplicate delivery or redelivery mid-send:** one email per revision. The second delivery sees the claim marker and exits 200. Test in Task 6.
2. **Manifest arrives before its episodes** (upload ordering, or a partial upload): the report service fails (500), so Pub/Sub retries, and sends nothing. The uploader uploads every episode a manifest references before the manifest itself. Tests in Tasks 2 and 6.
3. **Multi-hour merged episode:** memory stays bounded (one episode at a time, file-based ffmpeg, temp files deleted), and attachments fall back to links. Tests in Tasks 4 and 5.
4. **Fresh deploy on the OptiPlex with no cloud config:** the listener starts, uploads nothing, and logs one notice. With a bad or missing key it counts errors and keeps listening. Test in Task 2.
5. **Rev-2 manifest arrives while rev-1 is still processing:** the service reports only the highest revision present. An "updated" subject is used for rev > 1, and an older revision arriving late is ignored. Test in Task 3.

---

## File Structure

```
listener/src/chase/uploader.py      outbox -> GCS (create-only), ordering, sent/ retention
listener/src/chase/config.py        + gcs_bucket, gcs_credentials, upload_interval_seconds, uploaded_days
listener/src/chase/evidence.py      + metadata "clock" {source, timezone} (deferred minor)
report/                              uv project "chase-report", package chase_report
  src/chase_report/model.py          Manifest, EpisodeMeta (parse listener JSON; schema_version 1)
  src/chase_report/store.py          Store protocol + GcsStore + FakeStore (tests)
  src/chase_report/trigger.py        event parsing, revision selection, claim marker
  src/chase_report/assemble.py       copy to evidence/, MP3 via ffmpeg, hashes
  src/chase_report/render.py         Jinja2 HTML + SVG charts -> PDF (WeasyPrint)
  src/chase_report/templates/report.html.j2, letter.txt.j2
  src/chase_report/mail.py           compose EmailMessage (budget, links), SMTP sender
  src/chase_report/app.py            Flask: POST / (Pub/Sub push), structured logs
  Dockerfile
  tests/fixtures/                    generated by listener writer code (contract)
infra/envs/prod/*.tf                 APIs, AR, bucket, SAs/IAM, secrets, Cloud Run, Pub/Sub, WIF, monitoring
infra/envs/prod/grafana.tf           optional (var.grafana_enabled): alert rules + dashboard
infra/bootstrap.sh, infra/README.md, infra/smoke-iam.sh
.github/workflows/ci.yml             + report job, terraform job
.github/workflows/report-deploy.yml  build + push + deploy on main (skips until vars exist)
docs/adr/0004-optiplex-service-account-key.md, docs/adr/0005-report-trigger-and-idempotency.md
```

---

### Task 1: Report scaffold, models, and the listener↔report contract

**Files:**
- Create: `report/pyproject.toml`, `report/src/chase_report/{__init__,model}.py`, `report/tests/test_model.py`, `report/tests/fixtures/` (generated)
- Create: `listener/tests/test_contract.py` (writes the fixtures)
- Modify: `listener/src/chase/evidence.py` (adds `clock`), `listener/src/chase/service.py` (passes the timezone)

**Interfaces:**
- **Listener metadata gains** `"clock": {"source": "system clock (NTP-synced)", "timezone": "<tz>"}`. `write_episode(..., timezone: str = "UTC")` is a new trailing keyword. Service passes `settings.timezone`.
- **`listener/tests/test_contract.py`:**
  - `test_write_contract_fixtures` uses the real `write_episode` and `write_violation` with synthetic data (invented, not personal) to write `report/tests/fixtures/{episode_metadata.json, violation_manifest.json}`, plus a 2-episode set under `report/tests/fixtures/bucket/` laid out like the GCS bucket. With `CHASE_UPDATE_FIXTURES=1` it rewrites them. Otherwise it asserts the committed fixtures equal what the writer produces (modulo audio bytes and sha), so the two sides can't drift.
- **`chase_report.model`:**
  - `@dataclass(frozen=True) class Window: start: datetime; end: datetime; confidence: float; loudness_dbfs: float`
  - `@dataclass(frozen=True) class Gap: start: datetime; end: datetime; seconds: float`
  - `@dataclass(frozen=True) class EpisodeMeta: episode_id: str; start: datetime; end: datetime; clip_start: datetime; clip_end: datetime; barking_seconds: float; bursts: int; longest_gap_seconds: float; mean_loudness_dbfs: float; merged_from: int; windows: tuple[Window, ...]; device: str; model: str; confidence_threshold: float; sample_rate: int; duration_seconds: float; flac_sha256: str; complete: bool; gaps: tuple[Gap, ...]; timezone: str; clock_source: str`. The `classmethod from_json(d: dict) -> EpisodeMeta` raises `ValueError` on an unknown `schema_version`. A missing `clock` (Plan 1 files) defaults to `timezone="UTC"`, `clock_source="unknown"`.
  - `@dataclass(frozen=True) class Rule: name: str; confidence_threshold: float; episode_min_minutes: float; max_gap_seconds: float; min_separation_minutes: float; episodes_per_window: int; violation_window_hours: float; citation: str; caveat: str; addressee: str`
  - `@dataclass(frozen=True) class Manifest: violation_id: str; revision: int; window_start: datetime; window_end: datetime; episode_ids: tuple[str, ...]; rule: Rule; hourly_history: tuple[tuple[datetime, int], ...]`. The `classmethod from_json(d)` raises on schema mismatch.

- [ ] **Step 1: Scaffold the report project.** `report/` is a uv project with runtime deps `flask`, `gunicorn`, `jinja2`, `weasyprint`, `google-cloud-storage`, `google-auth`, `requests`, and the same dev tooling and ruff/pyright config as the listener.
- [ ] **Step 2: Write the failing tests.**
  - `test_contract.py::test_write_contract_fixtures` fails: there is no `clock` key and no fixtures yet.
  - In `report/tests/test_model.py`:
    - `test_episode_from_fixture`: fields parse, `timezone == "America/Chicago"`, `bursts` comes from `barking_bursts_estimated`, and datetimes are tz-aware UTC.
    - `test_manifest_from_fixture`: 2 episode ids, revision 1, rule caveat present, 720 history entries.
    - `test_unknown_schema_rejected`
    - `test_plan1_metadata_without_clock_defaults`
- [ ] **Step 3: Run them** → FAIL.
- [ ] **Step 4: Implement** the listener `clock` addition and `model.py`. Generate the fixtures with `CHASE_UPDATE_FIXTURES=1`.
- [ ] **Step 5: Run both suites** → PASS.
- [ ] **Step 6: Commit:** `feat(report): scaffold report service with listener contract fixtures`

### Task 2: Listener uploader

**Files:**
- Create: `listener/src/chase/uploader.py`, `listener/tests/test_uploader.py`
- Modify: `config.py`, `cli.py`, `metrics.py` (no new metric names; adds reason `upload`), `compose.yaml` (mounts `./secrets:/secrets:ro`), `.gitignore` (`secrets/`), `config/chase.example.yaml`

**Interfaces:**
- **New Settings fields:** `gcs_bucket: str | None = None`, `gcs_credentials: Path = Path("/secrets/listener-key.json")`, `upload_interval_seconds: float = 30.0`, `uploaded_days: int = 30`.
- **Errors:** `class UploadError(Exception)`.
- **Bucket abstraction:** `class BucketWriter(Protocol): def create(self, name: str, path: Path, content_type: str) -> bool`. It returns True if created and False if the object already existed (a 412), and raises `UploadError` on anything else. `GcsBucketWriter(bucket: str, credentials: Path)` uploads with `if_generation_match=0` and **never** calls `exists()`, `reload()` or list.
- **Uploader:** `class Uploader: __init__(self, outbox: Path, sent_dir: Path, writer: BucketWriter, metrics: Metrics)`.
  - `run_once(self) -> int` returns the number of entries completed.
  - **Episodes:** each `outbox/episodes/<id>/` uploads `audio.flac` (`audio/flac`) then `metadata.json`, then the directory moves to `sent_dir/episodes/<id>/`.
  - **Violations:** each `outbox/violations/<vid>/rev-<n>.json` is uploaded **only if** every `episode_ids` entry is already under `sent_dir/episodes/` (that is, uploaded). Otherwise it's skipped for now. After upload it moves to `sent_dir/violations/<vid>/`.
  - **Failures:** a `UploadError` counts `audio_errors{reason="upload"}`, leaves the entry in place, and continues with the others. `.tmp` directories are ignored.
- **Retention:** `prune_sent(sent_dir: Path, now: datetime, days: int) -> int` removes sent entries whose directory mtime is older than `days`.
- **Wiring:** `build_uploader(settings: Settings, metrics: Metrics) -> Uploader | None`. It returns None and logs one info line when `gcs_bucket` is unset. When the credentials file is missing or unreadable, it returns None, logs a warning and counts one upload error. `cli.run` starts a daemon thread that calls `run_once` every `upload_interval_seconds` while not stopped.
- **Gauge:** `chase_outbox_pending` already counts what remains in `outbox/`.

- [ ] **Step 1: Write the failing tests** (with a `FakeWriter` that records uploads and can be scripted to fail or return 412):
  - `test_episode_uploaded_then_moved`: names are `episodes/<id>/audio.flac` and then `episodes/<id>/metadata.json`, in that order. Afterwards the outbox entry is gone and `sent/episodes/<id>` exists.
  - `test_manifest_waits_for_its_episodes` (Review Focus 2): with a manifest whose 2nd episode fails to upload, the manifest is not uploaded on this run. On the next run, once both have succeeded, it is.
  - `test_412_counts_as_done`
  - `test_upload_error_counted_and_retried`
  - `test_tmp_dirs_ignored`
  - `test_prune_sent`
  - `test_disabled_without_bucket` (Review Focus 4): `build_uploader` returns None and nothing is raised.
  - `test_missing_key_is_counted_not_fatal`: returns None, and `audio_errors{upload} == 1`.
  - `test_existing_config_still_loads`: the shipped `config/chase.example.yaml` loads unchanged with `gcs_bucket is None`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS (listener suite).
- [ ] **Step 5: Commit:** `feat(listener): upload the evidence outbox to GCS with create-only credentials`

### Task 3: Trigger, revision selection, and the claim

**Files:** Create `report/src/chase_report/{store,trigger}.py`, `report/tests/test_trigger.py`, `report/tests/fakes.py`

**Interfaces:**
- **Store protocol:** `class Store(Protocol)` with:
  - `def read_bytes(self, name: str) -> bytes`
  - `def download(self, name: str, path: Path) -> None`
  - `def upload(self, name: str, path: Path, content_type: str) -> None`
  - `def put_bytes(self, name: str, data: bytes, content_type: str, if_absent: bool = False) -> bool` returns False if `if_absent` and the object exists
  - `def copy(self, src: str, dst: str) -> None`
  - `def list(self, prefix: str) -> list[str]`
  - `def exists(self, name: str) -> bool`
  - `def signed_url(self, name: str, hours: int) -> str`
- **Implementations:** `GcsStore(bucket: str)` signs URLs with IAM signBlob (`google.auth.compute_engine.IDTokenCredentials` is not used; it passes `service_account_email` + `access_token` to `generate_signed_url`). `tests/fakes.py: FakeStore` is backed by a dict, and `signed_url` returns `"https://signed.example/<name>"`.
- **Event parsing:** `def parse_push(envelope: dict) -> tuple[str, str] | None` returns `(bucket, object)` from a Pub/Sub push envelope (`message.attributes.bucketId/objectId`, with `eventType == "OBJECT_FINALIZE"`). Anything else returns None.
- **Object names:** `MANIFEST_RE = r"^violations/(v-\d{8}T\d{6}Z)/rev-(\d+)\.json$"`. `def manifest_key(name: str) -> tuple[str, int] | None`.
- **Revision selection:** `def latest_revision(store: Store, violation_id: str) -> int`, the highest `rev-N` under `violations/<vid>/`.
- **Claim:** `def claim(store: Store, violation_id: str, revision: int) -> bool` does `put_bytes("evidence/<vid>/rev-<n>/claimed.json", …, if_absent=True)`.
- **Decision:** `def decide(store, name) -> Decision`, where `Decision = Literal["ignore", "stale", "duplicate", "process"]`:
  - not a manifest → ignore;
  - `revision < latest_revision` → stale;
  - claim fails → duplicate;
  - otherwise → process.

- [ ] **Step 1: Failing tests:**
  - `test_parse_push_finalize`, `test_parse_push_other_events_ignored`
  - `test_non_manifest_ignored`
  - `test_stale_revision_ignored` (Review Focus 5): rev-1 arrives after rev-2 exists → stale.
  - `test_second_delivery_is_duplicate` (Review Focus 1): process, then the same name again → duplicate.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit:** `feat(report): pub/sub trigger parsing, revision selection and claim marker`

### Task 4: Evidence assembly and MP3

**Files:** Create `report/src/chase_report/assemble.py`, `report/tests/test_assemble.py`

**Interfaces:**
- **Errors:** `class MissingEvidence(Exception)`. It is raised when a manifest's episode is not in the bucket, so the service returns 500 and Pub/Sub retries.
- **Result types:**
  - `@dataclass(frozen=True) class EpisodeEvidence: meta: EpisodeMeta; flac_name: str; mp3_name: str; mp3_sha256: str; mp3_bytes: int`
  - `@dataclass(frozen=True) class Evidence: manifest: Manifest; episodes: tuple[EpisodeEvidence, ...]`
- **Assembly:** `def assemble(store: Store, manifest: Manifest, workdir: Path, encode: Callable[[Path, Path], None] = ffmpeg_mp3) -> Evidence`. For each episode, **one at a time**:
  1. check that `episodes/<id>/metadata.json` exists, or raise `MissingEvidence`;
  2. copy `audio.flac` and `metadata.json` to `evidence/<vid>/episodes/<id>/`;
  3. download the FLAC to `workdir`;
  4. encode the MP3;
  5. hash it in 1 MiB blocks;
  6. upload it to `evidence/<vid>/episodes/<id>/audio.mp3`;
  7. delete both local files before the next episode.

  It also copies the manifest to `evidence/<vid>/rev-<n>/manifest.json`.
- **Encoding:** `def ffmpeg_mp3(src: Path, dst: Path) -> None` runs `ffmpeg -nostdin -y -i src -ac 1 -b:a 64k dst` and raises `RuntimeError` with stderr on failure.

- [ ] **Step 1: Failing tests** (FakeStore with a fixture bucket, and a fake `encode` that writes N bytes and records peak concurrent temp files):
  - `test_copies_and_mp3_per_episode`
  - `test_missing_episode_raises` (Review Focus 2)
  - `test_one_episode_at_a_time` (Review Focus 3): never more than 2 temp files exist, and the workdir is empty at the end.
  - `@pytest.mark.ffmpeg test_ffmpeg_mp3_real`: encodes a 2 s synthetic FLAC. It's skipped if `ffmpeg` isn't on PATH, and runs in Docker and CI.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit:** `feat(report): assemble evidence and encode mp3s one episode at a time`

### Task 5: Report rendering (PDF) and email

**Files:**
- Create: `report/src/chase_report/{render,mail}.py`
- Create: `report/src/chase_report/templates/{report.html.j2,letter.txt.j2}`
- Create: `report/tests/test_render.py`, `report/tests/test_mail.py`

**Interfaces:**
- **Sender details:** `@dataclass(frozen=True) class Sender: name: str; address: str; phone: str; email: str; to_email: str; neighbor_addresses: tuple[str, ...]`, parsed by `Sender.from_json(str)` from the `REPORT_CONFIG` secret.
- **Report view:** `@dataclass(frozen=True) class ReportView` holds every display string, already localized to the manifest timezone, so templates contain no logic beyond loops. It is built by `def build_view(evidence: Evidence, sender: Sender, links: dict[str, str], today: date) -> ReportView`. Its fields:
  - `title`, `reference`, `addressee`;
  - `headline` (e.g. "On Saturday, November 8, 2026, 2 separate episodes of sustained barking totalling 44 minutes were recorded within 24 hours.");
  - `episodes` rows, each with: `#`, local start, local end, duration, barking minutes, est. bursts, longest gap, mean dBFS, `complete`/gap text, FLAC sha, MP3 sha;
  - `source_line` ("from neighboring properties, including: …");
  - `rule_text`, `citation`, `caveat`;
  - `timeline_svgs` and `hourly_svg` (inline SVG strings);
  - `recording_details`: device, model, threshold, clock source, timezone.
- **Rendering:**
  - `def render_html(view: ReportView) -> str` uses Jinja2 with autoescaping on.
  - `def render_pdf(html: str, out: Path) -> None` uses WeasyPrint.
  - `def letter_text(view: ReportView) -> str` produces the plain-text letter for the email body.
- **Charts:**
  - `def timeline_svg(meta: EpisodeMeta) -> str` draws barking windows as red marks on a strip, with tick labels in local time.
  - `def hourly_svg(manifest: Manifest, day: date) -> str` covers the 24 local hours of the violation day: bars for that day, and a dashed line for the 30-day average at each hour, computed from `hourly_history`.
- **Email:** `def compose(view: ReportView, pdf: Path, mp3s: list[tuple[str, Path]], links: dict[str, str], sender: Sender, revision: int, limit_encoded_mb: float = 15.0) -> EmailMessage`.
  - From and To are `sender.email` → `sender.to_email`.
  - **Subject:** `"Barking dog complaint: <Mon D, YYYY> (<N> episodes)"`, with the suffix `" (updated)"` when `revision > 1`.
  - **Body:** `letter_text`, followed by a "Recordings" section that lists the FLAC links (marked as expiring in 7 days).
  - **Attachments:** the PDF always. MP3s are attached only if `ceil(raw * 4/3)` summed over the PDF and all MP3s is within the limit. Otherwise the body also lists MP3 links.
- **Sending:** `class SmtpSender: __init__(self, user: str, app_password: str, host="smtp.gmail.com", port=465)`; `send(msg) -> None` over SSL.

- [ ] **Step 1: Failing tests** (fixture evidence, Sender "Pat Example / 123 Sample St"):
  - `test_headline_uses_minutes_and_local_date`
  - `test_never_says_barks`: rendered HTML and letter text contain "barking bursts" and no word-boundary "barks".
  - `test_no_single_address_named_as_source`: `source_line` starts with "from neighboring properties" and lists every configured address.
  - `test_caveat_and_citation_present`
  - `test_gaps_disclosed`: an episode with `complete=False` shows its gap.
  - `test_both_hashes_listed`
  - `test_times_are_local`: 12:00Z renders as 6:00 AM.
  - `test_subject_updated_on_rev2`
  - `test_attach_mp3_when_under_budget`
  - `test_links_when_over_budget` (Review Focus 3): a large MP3 means the PDF only is attached and the MP3 links appear.
  - `test_budget_counts_base64`: raw sizes summing to 14 MB exceed a 15 MB encoded limit.
  - `@pytest.mark.pdf test_render_pdf_real`: WeasyPrint writes a non-empty PDF whose first bytes are `%PDF`. It's skipped if WeasyPrint can't load its libraries, and runs in Docker and CI.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit:** `feat(report): letter + evidence-packet PDF and ready-to-forward email`

### Task 6: Service wiring, Docker image, and a sample report

**Files:**
- Create: `report/src/chase_report/app.py`, `report/Dockerfile`, `report/tests/test_app.py`
- Create: `report/scripts/render_sample.py`, `docs/samples/` (sample PDF + .eml from synthetic fixtures)

**Interfaces:**
- **Processing:** `def process(store: Store, name: str, sender: Sender, mailer, workdir: Path, today: date, limit_mb: float) -> str` returns the outcome (`ignored|stale|duplicate|sent`):
  1. `decide`;
  2. load the manifest;
  3. `assemble`;
  4. signed URLs, 7 days, for the FLACs (and MP3s if not attached);
  5. build the view;
  6. render the PDF to workdir, then upload it to `evidence/<vid>/rev-<n>/report.pdf`;
  7. compose;
  8. send;
  9. write `evidence/<vid>/rev-<n>/sent.json` (`{sent_at, message_id, attachments}`).

  On **any** exception after the claim, it deletes the claim marker (so the retry can run) and re-raises. If the email was already sent, it keeps the claim and logs `report_failed_after_send` (it never sends twice).
- **HTTP app:** `create_app(store_factory, sender_loader, mailer_factory) -> Flask`. `POST /` parses the Pub/Sub envelope. It returns 204 for ignored/stale/duplicate/sent and 500 on failure, which makes Pub/Sub retry. Every request logs one JSON line with `event` in `report_ignored|report_stale|report_duplicate|report_sent|report_failed`. Cloud Logging reads `severity` from it.
- **Environment:** `REPORT_BUCKET`, `REPORT_CONFIG` (secret JSON), `GMAIL_APP_PASSWORD` (secret), `ATTACHMENT_LIMIT_MB` (default 15).
- **Image:** `python:3.12-slim` with `ffmpeg`, `libpango-1.0-0`, `libpangoft2-1.0-0` and `fonts-dejavu-core`. It runs uv sync frozen, as a non-root user, with `CMD gunicorn -b :$PORT -w 1 --threads 1 --timeout 600 chase_report.app:app`.

- [ ] **Step 1: Failing tests:**
  - `test_end_to_end_sends_one_email` (FakeStore with the fixture bucket, FakeMailer, fake encode): outcome `sent`; one message; `report.pdf`, `sent.json` and the MP3s exist.
  - `test_redelivery_does_not_resend` (Review Focus 1)
  - `test_failure_releases_claim_and_returns_500`: the encoder raises, so the response is 500, there's no claim, and nothing is sent. The next delivery succeeds.
  - `test_missing_episode_returns_500_no_email` (Review Focus 2)
  - `test_failure_after_send_keeps_claim`
  - `test_push_auth_shape`: a malformed body gets 400.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: In Docker:**
  - `docker build -t chase-report:local report/`
  - run the full report suite inside the image (pdf/ffmpeg markers enabled)
  - `render_sample.py` writes `docs/samples/sample-report.pdf` and `sample-email.eml` from the synthetic fixtures. Open the PDF and check it visually: the letter on page 1, the packet after it, readable SVG charts.
- [ ] **Step 6: Commit:** `feat(report): cloud run service, image, and a synthetic sample report`

### Task 7: Terraform

**Files:** Create `infra/envs/prod/{versions,main,variables,outputs,storage,listener,report,pubsub,wif,monitoring}.tf`, `terraform.tfvars.example`, `.gitignore`

**Requirements:**
- **Backend:** `gcs` with prefix `envs/prod`; the bucket is supplied at init. Pin `required_version >= 1.5.7, < 2.0.0` and google `~> 6.0`.
- **APIs:** run, pubsub, storage, secretmanager, artifactregistry, iam, iamcredentials, sts, monitoring, logging, cloudresourcemanager, serviceusage. Each with `disable_on_destroy = false`.
- **Artifact Registry:** a `chase` Docker repository.
- **Bucket** `${project_id}-chase-evidence`:
  - uniform access, public access prevention `enforced`;
  - lifecycle delete rules: `episodes/` and `violations/` age 30, `evidence/` age 365.
- **Listener service account** `chase-listener`: `roles/storage.objectCreator` on the bucket, with an IAM condition limiting it to object names starting with `projects/_/buckets/<bucket>/objects/episodes/` or `…/violations/`. No key in Terraform.
- **Report service account** `chase-report`:
  - `roles/storage.objectAdmin` on the bucket;
  - `secretAccessor` on the two secrets;
  - `roles/iam.serviceAccountTokenCreator` on itself (to sign URLs).
- **Secrets:** shells for `chase-report-config` and `chase-gmail-app-password`, no versions (values are added in bootstrap).
- **Cloud Run v2 service** `report`:
  - ingress `INGRESS_TRAFFIC_INTERNAL_ONLY`;
  - min 0, max 1 instances; `max_instance_request_concurrency = 1`; timeout 600 s;
  - 2 vCPU, 4 Gi, `cpu_idle = true`;
  - env from the secrets;
  - `lifecycle.ignore_changes` on the image (CI deploys; the tower pattern);
  - initial image from `var.report_image` (default: a public placeholder image so the first apply works before CI pushes).
- **Pub/Sub:**
  - topic `chase-violations`;
  - `google_storage_notification` on the bucket: `object_name_prefix = "violations/"`, event `OBJECT_FINALIZE`, JSON payload. The GCS service agent gets `pubsub.publisher` on the topic.
  - invoker service account `chase-push` with `run.invoker` on the service;
  - push subscription:
    - OIDC token (audience = service URL), `ack_deadline_seconds = 600`;
    - retry policy 10 s–600 s;
    - dead-letter topic `chase-violations-dead` after 10 attempts. The Pub/Sub service agent gets publisher on the dead-letter topic and subscriber on the subscription.
- **WIF** (copied from tower):
  - a pool and provider whose `attribute_condition` restricts to `var.github_repository`;
  - CI service account `chase-ci`, usable only from `refs/heads/main`;
  - grants: `artifactregistry.writer` on the repository, `run.developer` on the `report` service, `iam.serviceAccountUser` on `chase-report`.
- **Monitoring:**
  - email notification channel (`var.alert_email`);
  - log-based metric for `resource.type="cloud_run_revision" AND resource.labels.service_name="report" AND jsonPayload.event=("report_failed" OR "report_failed_after_send")`;
  - an alert policy on it (any occurrence in 5 min);
  - an alert on the dead-letter topic's `num_undelivered_messages > 0`.
- **Outputs:** bucket name, listener service account email, report service URL, WIF provider name, CI service account email, Artifact Registry URL.

- [ ] **Step 1:** Write the files.
- [ ] **Step 2:** `terraform fmt -check -recursive infra` and `cd infra/envs/prod && terraform init -backend=false && terraform validate` → "Success! The configuration is valid."
- [ ] **Step 3: Commit:** `feat(infra): terraform for evidence bucket, report service, pub/sub trigger, WIF and alerts`

### Task 8: CI and the deploy workflow

**Files:**
- Modify: `.github/workflows/ci.yml`. Adds:
  - `report`: apt `ffmpeg libpango-1.0-0 libpangoft2-1.0-0`, then uv sync, ruff, pyright, pytest with all markers;
  - `terraform`: setup-terraform 1.5.7, then `fmt -check` and `init -backend=false && validate`.
- Create: `.github/workflows/report-deploy.yml`:
  - **Trigger:** `workflow_run` of CI on main, success only.
  - **Guard:** if the repo variables `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `GCP_ARTIFACT_REGISTRY_URL` and `GCP_REGION` aren't all set, print a notice and exit 0, so merging before bootstrap stays green.
  - **Steps:** WIF auth, build `report/` and tag it `<registry>/report:<sha>`, push, then `gcloud run deploy report --image … --region …`.
  - **Concurrency:** group `report-deploy`, no cancel.

- [ ] **Step 1:** Write the workflows. **Step 2:** `actionlint` if available (otherwise `python -c 'import yaml; yaml.safe_load(...)'`), and push the branch to watch CI go green.
- [ ] **Step 3: Commit:** `ci: report tests, terraform validation, and report deploy on main`

### Task 9: Bootstrap, IAM smoke test, docs, and ADRs

**Files:**
- Create: `infra/bootstrap.sh`, `infra/smoke-iam.sh`, `infra/send-test-violation.sh`, `infra/README.md`
- Create: `docs/adr/0004-optiplex-service-account-key.md`, `docs/adr/0005-report-trigger-and-idempotency.md`
- Modify: `README.md`, `docs/running-on-the-optiplex.md`

**`infra/bootstrap.sh`** is a guided, idempotent wizard with prompts and confirmations. It never runs `apply` itself, only prints the command. Its stages:
1. prerequisites;
2. create or select the project;
3. link billing;
4. state bucket (versioned);
5. ADC login and quota project;
6. `terraform.tfvars` from the example;
7. `terraform init`;
8. pause: "run `terraform plan` / `terraform apply` yourself";
9. secret values: prompt for the Gmail app password (the account must have 2-Step Verification on), and build `REPORT_CONFIG` JSON from prompts;
10. create the listener key into `./secrets/listener-key.json` with `gcloud iam service-accounts keys create`, and print the `scp` line to put it on the OptiPlex;
11. print the `gh variable set` commands for the repo variables, without running them.

**`infra/smoke-iam.sh <bucket> <key>`** activates the key in a temporary gcloud config, then checks:
- `episodes/` and `violations/` uploads succeed;
- `evidence/` upload, read and list are denied.

It prints PASS/FAIL for each.

**ADRs:**
- **0004** records the key exception: why, its scope, kept out of state, and rotation.
- **0005** records the trigger and idempotency design: Pub/Sub push instead of Eventarc, the claim marker, the dead letter, max 1 instance and concurrency 1.

**Docs:**
- The runbook gains an "Enable cloud evidence" section: copy the key to `~/chase/secrets/`, set `gcs_bucket` in `chase.yaml`, run `make restart`.
- It also gains a **morning checklist**: bootstrap, apply, secrets, key, GitHub variables, smoke test, then **one end-to-end TEST report** (`infra/send-test-violation.sh` uploads the synthetic fixture bucket with a "TEST" violation id prefix).
- The README updates its status table and roadmap.

- [ ] **Step 1:** Write them. **Step 2:** `shellcheck` all scripts in a Linux container; run `bootstrap.sh --help` and a dry run. **Step 3: Commit:** `docs(infra): bootstrap wizard, IAM smoke test, ADRs 0004-0005, cloud runbook`

### Task 10 (optional, last): Grafana alert rules as code

**Files:** Create `infra/envs/prod/grafana.tf` (`count = var.grafana_enabled ? 1 : 0`; grafana provider configured from `var.grafana_url` and `var.grafana_auth`, both sensitive).

**Requirements:**
- **Alert rules:**
  - `time() - chase_heartbeat_timestamp_seconds > 900` (listener silent);
  - `time() - chase_last_tick_success_timestamp_seconds > 900`;
  - `increase(chase_audio_errors_total[15m]) > 20`;
  - `chase_outbox_pending > 0 for 2h` (uploads stuck).
- **Contact point:** email to `var.alert_email`.
- **Dashboard** from `grafana/dashboard.json`.
- **Default:** disabled, so validation and plan need no Grafana token.

- [ ] **Step 1:** Write it. **Step 2:** `terraform validate`. **Step 3: Commit:** `feat(infra): optional grafana alert rules and dashboard as code`
