from __future__ import annotations

from dutybot.notify import CooldownBook


def test_cooldown_allow_deny():
    t = {"now": 0.0}

    def now() -> float:
        return t["now"]

    book = CooldownBook(now=now)
    assert book.allow("cpu", 15 * 60)
    assert not book.allow("cpu", 15 * 60)
    t["now"] = 14 * 60
    assert not book.allow("cpu", 15 * 60)
    t["now"] = 15 * 60
    assert book.allow("cpu", 15 * 60)


def test_ssh_fail_per_ip_independent():
    t = {"now": 100.0}

    def now() -> float:
        return t["now"]

    book = CooldownBook(now=now)
    assert book.allow("ssh-fail:1.1.1.1", 600)
    assert book.allow("ssh-fail:2.2.2.2", 600)
    assert not book.allow("ssh-fail:1.1.1.1", 600)
    t["now"] = 100.0 + 600
    assert book.allow("ssh-fail:1.1.1.1", 600)
