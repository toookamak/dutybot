from __future__ import annotations

from pathlib import Path

import pytest

from dutybot.config import (
    WatchList,
    WatchService,
    any_channel_complete,
    hash_password,
    load_config,
    telegram_complete,
    verify_password,
    web_complete,
)


def test_telegram_needs_both():
    assert not telegram_complete("", "1")
    assert not telegram_complete("tok", "")
    assert not telegram_complete(None, None)
    assert telegram_complete("tok", "12345")


def test_web_needs_user_and_hash():
    assert not web_complete("", "hash")
    assert not web_complete("user", "")
    assert web_complete("user", "pbkdf2_sha256$1$x$y")


def test_any_channel_complete_false_when_empty():
    assert not any_channel_complete("", "", "", "")
    assert not any_channel_complete("t", "", "u", "")


def test_any_channel_complete_telegram_only():
    assert any_channel_complete("t", "1", "", "")


def test_any_channel_complete_web_only():
    assert any_channel_complete("", "", "u", "h")


def test_password_hash_roundtrip():
    stored = hash_password("correct horse")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("correct horse", "not-a-hash")


def test_watchlist_roundtrip_empty(tmp_state: Path):
    path = tmp_state / "watch.json"
    wl = WatchList(services=[], path=path)
    wl.save()
    loaded = WatchList.load(path)
    assert loaded.services == []
    assert loaded.to_dict()["services"] == []


def test_watchlist_add_service(tmp_state: Path):
    path = tmp_state / "watch.json"
    wl = WatchList(path=path)
    wl.add(WatchService(id="example", name="Example", unit="example.service", probe=None))
    wl.save()
    loaded = WatchList.load(path)
    assert len(loaded.services) == 1
    assert loaded.services[0].unit == "example.service"


def test_invalid_unit_raises():
    with pytest.raises(ValueError):
        WatchService.from_dict({"unit": "not a unit", "id": "x", "name": "x"})
    with pytest.raises(ValueError):
        WatchService.from_dict({"unit": "evil.service;reboot", "id": "x", "name": "x"})


def test_load_config_channel_flags(tmp_state: Path):
    env = tmp_state / "env"
    env.write_text("WEB_BIND=127.0.0.1\nWEB_PORT=8787\n", encoding="utf-8")
    cfg = load_config(env, require_channel=False)
    assert not cfg.telegram_ok
    assert not cfg.web_ok
    assert not cfg.any_channel

    env.write_text("BOT_TOKEN=tok\nALLOWED_CHAT_ID=42\n", encoding="utf-8")
    cfg = load_config(env, require_channel=True)
    assert cfg.telegram_ok
    assert not cfg.web_ok
    assert cfg.any_channel


def test_refuse_without_channel(tmp_state: Path):
    env = tmp_state / "env"
    env.write_text("WEB_BIND=127.0.0.1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        load_config(env, require_channel=True)
    assert "refuses to start" in str(exc.value)
