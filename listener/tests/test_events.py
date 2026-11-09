import json

from chase.events import emit


def test_emit_writes_json_line(capsys):
    emit("episode", episode_id="ep-1", duration_seconds=661.0, violation_id=None)
    line = capsys.readouterr().out.strip()
    rec = json.loads(line)
    assert rec["event"] == "episode"
    assert rec["episode_id"] == "ep-1"
    assert rec["violation_id"] is None
    assert rec["ts"].endswith("Z")
    assert "\n" not in line
