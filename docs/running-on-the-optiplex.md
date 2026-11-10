# Running chase on the OptiPlex

Plan 1 runs entirely on the OptiPlex: the listener detects barking, logs it, writes evidence to
a local outbox, and Grafana Alloy ships metrics and events to Grafana Cloud. (Uploading the
evidence and emailing reports arrives with Plan 2.)

## 1. Prerequisites

- Linux with Docker Engine + the Compose plugin.
- **A synced clock.** Evidence timestamps depend on it:
  ```bash
  timedatectl   # expect: System clock synchronized: yes
  ```
- A USB microphone, pointed at the yards you care about.

## 2. Find the microphone

```bash
arecord -l
# card 2: Device [USB Audio Device], device 0: USB Audio [USB Audio]
```

Set `audio_device` to **part of the mic's name** (for example `USB`). It's required: the
system default is usually the OptiPlex's built-in input, which would record silence while
looking healthy. Don't use the `arecord` card number, because sounddevice numbers devices
differently. A name also keeps working if the mic is re-plugged and renumbered.

Check that the host's `audio` group matches the container's (GID 29 on Debian-based images):

```bash
getent group audio    # audio:x:29:...
```

If the GID differs, set `AUDIO_GID=<number>` in `.env`, or the listener won't be able to open
the mic. Don't edit `compose.yaml` itself: local edits to tracked files make auto-update
refuse to run.

## 3. Configure

```bash
cp config/chase.example.yaml config/chase.yaml   # git-ignored
cp .env.example .env                              # git-ignored
```

- `config/chase.yaml`: set `audio_device`; keep `rules: {preset: san-antonio}` or override
  thresholds there.
- `.env`: Grafana Cloud Prometheus and Loki URLs and users, plus an access-policy token with
  `metrics:write` and `logs:write`. Use the **full push URLs**: the Prometheus one ends in
  `/api/prom/push`, and for Loki you must append `/loki/api/v1/push` to the base URL shown on
  the Details page (a `405 Method Not Allowed` from `loki.write` means it's missing).

## 4. Start

```bash
docker compose up -d --build
docker compose logs -f listener      # JSON events: listener_started, episode, violation, ...
curl -s localhost:9100/metrics | grep chase_   # only if you publish the port; or:
docker compose exec listener curl -s localhost:9100/metrics | grep chase_heartbeat
```

If the mic is unplugged, the listener keeps retrying, with `chase_audio_errors_total{reason="capture"}`
counting each attempt, and picks the mic back up when it returns.

Quick check: play a dog-bark video near the mic. `chase_barking_windows_total` should rise,
and backlog clips should appear:

```bash
docker compose exec listener ls /data/backlog | tail
```

## Everyday commands

`make help` lists them (install make with `sudo apt install make` if it's missing):

| Command | What it does |
|---|---|
| `make up` / `make down` / `make restart` | start, stop, restart after a config change |
| `make status` / `make watch` | barking, episodes, and health metrics right now / live |
| `make logs` | follow the listener's events |
| `make backlog` / `make episodes` / `make violations` | what's been recorded |
| `make replay SINCE=2026-11-01 PRESET=config/presets/x.yaml` | re-run the rules over history |
| `make test-mic` | record 5 s from the mic and play it back |
| `make deploy` | pull from git and redeploy now |

## Auto-update from git

```bash
make install-cron     # every 15 minutes; CRON_SCHEDULE='*/5 * * * *' to change
make cron-log         # recent runs
make uninstall-cron
```

`deploy/auto-update.sh` fetches the branch you have checked out. If there's a new commit, it
fast-forwards and runs `docker compose up -d --build`. It does nothing when nothing changed. It
refuses (and logs why) if tracked files have local edits or if the remote history was
rewritten, and it never discards anything. `config/chase.yaml` and `.env` are git-ignored, so
your settings never block it. Your user needs to be in the `docker` group for cron to run
compose.

Whatever lands on that branch deploys within 15 minutes, so push only what you want running.
Once PR #14 is merged, switch the OptiPlex to `main` (`git checkout main`) so it follows main.

## Cloud evidence (Plan 2)

Once the cloud side is set up (see [infra/README.md](../infra/README.md)):

```bash
mkdir -p ~/chase/secrets
# from the machine where you ran infra/bootstrap.sh:
#   scp secrets/listener-key.json wizard@orb:~/chase/secrets/listener-key.json
nano config/chase.yaml        # add:  gcs_bucket: <your-project>-chase-evidence
make restart
make logs                     # listener_started ... "upload": true
```

From then on, every episode and violation manifest is uploaded within about 30 seconds and moved
from `/data/outbox/` to `/data/sent/` (kept 30 days). A violation becomes a report email in your
inbox. If uploads fail (network, key), the listener keeps listening, counts
`chase_audio_errors_total{reason="upload"}`, and retries.

## 5. Dashboard

Grafana Cloud -> Dashboards -> New -> Import -> upload `grafana/dashboard.json`, then pick your
Prometheus and Loki data sources.

## 6. Tune the rules against real history

`chase replay` re-runs the rules over everything logged so far, read-only:

```bash
docker compose run --rm listener replay --config /config/chase.yaml --presets /config/presets \
  --since 2026-11-01
# Try stricter or looser thresholds without touching the live service:
docker compose run --rm listener replay --config /config/chase.yaml --presets /config/presets \
  --preset /config/presets/my-experiment.yaml
```

When you like a result, put those thresholds in `config/chase.yaml` and
`docker compose restart listener`.

## Where things live (the `chase-data` volume)

| Path | What | Leaves the machine? |
|---|---|---|
| `/data/chase.sqlite` | bark log (source of truth) | no |
| `/data/spool/` | last ~2 h of audio, 1-min FLAC segments | no |
| `/data/outbox/episodes/<id>/` | episode FLAC (30 s pre/post-roll) + metadata.json | Plan 2 uploads it |
| `/data/outbox/violations/<id>/rev-N.json` | violation manifests | Plan 2 uploads it |
| `/data/backlog/YYYY-MM-DD/` | one short clip per barking burst (max 5 s, plus one per minute of a long burst), kept 90 days for the v2 classifier | no |

Each episode's `metadata.json` lists any stretch with no recorded audio (`audio.gaps`, with
`audio.complete: false`), for example from a restart in the middle of a bout. Missing audio is
never passed off as silence. `docker compose stop` gives the listener 30 s to flush its
buffered audio.
