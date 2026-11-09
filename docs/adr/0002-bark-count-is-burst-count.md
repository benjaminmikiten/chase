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
