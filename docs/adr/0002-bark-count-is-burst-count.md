# 0002. Bark count is a burst count

- **Status:** Accepted
- **Date:** 2026-11-09

## Decision

YAMNet scores ~1 s windows, not individual barks. The "bark count" chase reports is the number of
**bursts**: maximal sequences of barking windows separated by no more than one hop (0.48 s).
It is labelled "estimated" everywhere it appears. **Barking seconds** (the union of barking
windows) is the primary measure.

## Why

Onset detection inside windows would add a second audio model to tune and test. Burst counting
falls straight out of the window log, is deterministic, and is honest about its precision.

## Update (2026-11-09)

Review showed bursts badly undercount steady barking: with overlapping ~1 s windows, barking
about a second apart scores as barking in every window, so a 30-minute episode can be "~2
bursts". Ben decided: everything user-facing says **"barking bursts"** (never "barks"), and
**barking minutes** is the headline number. Metric names (`chase_barks_total`, ...) are kept for
stability; their help text says bursts. Counting individual barks is a follow-up issue.
