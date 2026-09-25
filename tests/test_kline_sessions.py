"""Who holds each K-line module's session: ours, another tester's, nobody."""

from __future__ import annotations

from src.collection.kline.sessions import ModuleSessions


def test_a_foreign_claim_outranks_ours_on_that_module_only() -> None:
    sessions = ModuleSessions()
    sessions.claim(0x28, "kline", now=0.0)
    assert sessions.owner(0x28, now=0.0) == "kline"
    sessions.claim(0x28, "foreign", ttl_seconds=10.0, now=0.0)
    assert sessions.owner(0x28, now=1.0) == "foreign"
    assert sessions.owner(0x10, now=1.0) == "none"


def test_a_foreign_claim_expires_and_renewing_pushes_it_out() -> None:
    sessions = ModuleSessions()
    sessions.claim(0x10, "foreign", ttl_seconds=10.0, now=0.0)
    sessions.claim(0x10, "foreign", ttl_seconds=10.0, now=8.0)
    assert sessions.foreign_active(now=17.0)
    assert not sessions.foreign_active(now=18.0)
    assert sessions.owner(0x10, now=18.0) == "none"


def test_another_tester_on_any_module_is_on_the_line() -> None:
    """K-line is one wire: a scan tool on the ABS blocks an ECM read too."""
    sessions = ModuleSessions()
    assert not sessions.foreign_active(now=0.0)
    sessions.claim(None, "foreign", ttl_seconds=5.0, now=0.0)  # attached mid-session
    assert sessions.foreign_active(now=1.0)
    assert sessions.owner(None, now=1.0) == "foreign"


def test_our_claim_holds_until_released_and_release_is_per_holder() -> None:
    sessions = ModuleSessions()
    sessions.claim(0x28, "kline", now=0.0)
    sessions.claim(0x28, "foreign", ttl_seconds=5.0, now=0.0)
    sessions.release(0x28, "kline")
    assert sessions.owner(0x28, now=1.0) == "foreign"
    assert sessions.owner(0x28, now=1e9) == "none"
    sessions.release(0x28, "kline")  # releasing nothing is not an error


def test_snapshot_names_addresses_in_hex_and_unknown_last() -> None:
    sessions = ModuleSessions()
    sessions.claim(None, "foreign", now=0.0)
    sessions.claim(0x28, "kline", now=0.0)
    sessions.claim(0x10, "foreign", now=0.0)
    assert list(sessions.snapshot(now=0.0).items()) == [
        ("10", "foreign"),
        ("28", "kline"),
        ("unknown", "foreign"),
    ]
    sessions.reset()
    assert sessions.snapshot(now=0.0) == {}
