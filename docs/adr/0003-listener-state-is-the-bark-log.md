# 0003. Listener state is the bark log

- **Status:** Accepted
- **Date:** 2026-11-09

## Decision

All listener state is derived from the SQLite bark log (`data_dir/chase.sqlite`). Every tick
recomputes runs, episodes and violations from the log with the pure rule engine. Episode and
violation IDs are deterministic (`ep-`/`v-` + first start time, UTC), and every write
(`add_window`, `save_episode`, `save_violation`, outbox files) is idempotent. Evidence is
written before the log row, so a crash between the two re-runs as a no-op write.

## Consequences

- A restart at any point loses nothing and duplicates nothing: no episode or violation is
  double-counted, and no manifest revision is repeated.
- Evidence for an episode is written up to `min_separation_minutes` after barking stops,
  because until then the episode could still merge with later barking.
- The spool never prunes audio needed by an open run or by an episode that has ended but
  isn't finalized yet.
