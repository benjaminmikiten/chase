# chase

> No job's too big, no pup's too loud.

`chase` listens for dog barking through a USB microphone, charts it on a Grafana Cloud dashboard, and when barking crosses a configurable noise-ordinance threshold, packages the audio and a ready-to-forward PDF complaint.

Status: design phase. See [`docs/superpowers/specs/2026-11-09-chase-design.md`](docs/superpowers/specs/2026-11-09-chase-design.md).

## Development

```bash
cd listener
uv sync
uv run pytest            # unit tests
uv run ruff check && uv run ruff format --check && uv run pyright
```
