from __future__ import annotations

import json
from pathlib import Path

from dutybot.config import WatchList, WatchService, parse_probe, slug_id


def test_default_empty_services(tmp_state: Path):
    wl = WatchList.load(tmp_state / "watch.json")
    assert wl.services == []
    assert wl.to_dict()["services"] == []


def test_watch_json_roundtrip(tmp_state: Path):
    path = tmp_state / "watch.json"
    wl = WatchList(path=path)
    wl.add(
        WatchService(
            id="example",
            name="Example",
            unit="example.service",
            probe="127.0.0.1:8080",
        )
    )
    wl.thresholds = {"poll_sec": 12}
    wl.save()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["services"][0]["unit"] == "example.service"
    assert "hermes" not in json.dumps(raw).lower()
    loaded = WatchList.load(path)
    assert len(loaded.services) == 1
    s = loaded.services[0]
    assert s.id == "example"
    assert s.name == "Example"
    assert s.unit == "example.service"
    assert s.probe == "127.0.0.1:8080"
    assert loaded.merged_thresholds()["poll_sec"] == 12
    loaded.remove_id("example")
    loaded.save()
    again = WatchList.load(path)
    assert again.services == []


def test_parse_probe_and_slug():
    assert parse_probe(None) is None
    assert parse_probe("-") is None
    assert parse_probe("127.0.0.1:90") == "127.0.0.1:90"
    assert slug_id("foo.service") == "foo"
    try:
        parse_probe("no-port")
        assert False
    except ValueError:
        pass
