# 0003. Listener state is the bark log

- **Status:** Accepted
- **Date:** 2026-11-09

## Decision

All listener state is derived from the SQLite bark log (`data_dir/chase.sqlite`). Every tick
recomputes runs, episodes and violations from the log with the pure rule engine. Episode and
violation IDs are deterministic (`ep-`/`v-` + first start time, UTC), and every write
(`add_window`, `save_episode`, `save_violation`, outbox files) is idempotent. Evidence is
written before the log row, so a crash between the two re-runs as a no-op write.

Each tick looks back a fixed `violation_window_hours + 2 h`. It does not resume from the last
saved episode, so an episode whose evidence write failed is retried on every tick (and after a
restart) until it ages out. Episodes that could be cut off by the lookback edge, or that
overlap an already-saved episode, are ignored.

An episode is **final** only when the settle time (`max(max_gap, min_separation)`) has passed
**and** no bout still in progress started within `min_separation_minutes` of its end. Otherwise
that bout could still qualify and merge with it.

## Consequences

- A restart at any point loses nothing and duplicates nothing: no episode or violation is
  double-counted, and no manifest revision is repeated.
- Evidence for an episode is written up to `min_separation_minutes` after barking stops,
  because until then the episode could still merge with later barking.
- The spool never prunes audio needed by an open run or by an episode that has ended but
  isn't finalized yet.
- **Downtime counts as a gap.** If the listener is down longer than `max_gap_seconds` in the
  middle of a bout, the bout splits there. The clip's metadata records any span with no audio
  (`audio.gaps`, `audio.complete`), so silence is never passed off as recorded audio.
- Live results always match `chase replay` over the same log, and a test checks this,
  including across random restarts.
