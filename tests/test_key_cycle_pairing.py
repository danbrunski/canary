"""Pairing by key cycle (src/control/key_cycle_pairing.py).

ACC -> ON -> ACC, read from the classified vehicle state: ON is the ECM
answering OBD (``ignition_only`` / ``engine_running``); anything else live is
ACC. The car is the proof, so the interesting cases are the ones where it
must *not* approve: two requests at once, a request made after the key was
already ON, ON held like driving, and states that are not the car at all.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.control.key_cycle_pairing import (
    MAX_ON_S,
    MAX_PENDING,
    REQUEST_TTL_S,
    KeyCycleDetector,
    KeyCyclePairing,
)
from src.control.paired_devices import PairedDevices
from src.provisioning.gate import read_window_remaining

ACC, ON = "engine_off", "ignition_only"


class Car:
    """A scripted key and clock driving one coordinator."""

    def __init__(self, tmp_path: Path) -> None:
        self.state: str | None = ACC
        self.now = 1000.0
        self.events: list[str] = []
        self.window = tmp_path / "provision_window"
        self.devices = PairedDevices(tmp_path / "devices.json")
        self.pairing = KeyCyclePairing(
            self.devices,
            lambda: self.state,
            window_path=self.window,
            publish=lambda action, result, payload: self.events.append(action),
            clock=lambda: self.now,
        )

    def hold(self, state: str | None, seconds: float = 2.0) -> None:
        self.state = state
        for _ in range(int(seconds)):
            self.now += 1
            self.pairing.tick()

    def cycle(self, on_seconds: float = 5.0) -> None:
        self.hold(ACC)
        self.hold(ON, on_seconds)
        self.hold(ACC)


@pytest.fixture
def car(tmp_path: Path) -> Car:
    return Car(tmp_path)


# --- the detector ---------------------------------------------------------


def _feed(detector: KeyCycleDetector, states: list[str], start: float = 0.0) -> list[float]:
    done = []
    for i, state in enumerate(states):
        began = detector.update(state, start + i)
        if began is not None:
            done.append(began)
    return done


def test_acc_on_acc_completes() -> None:
    assert _feed(KeyCycleDetector(), [ACC, ON, ON, ACC]) == [1.0]


def test_engine_running_counts_as_on() -> None:
    assert _feed(KeyCycleDetector(), ["parked", "engine_running", "parked"]) == [1.0]


def test_on_first_seen_without_acc_does_not_count() -> None:
    detector = KeyCycleDetector()
    assert _feed(detector, [ON, ON, ACC]) == []
    assert _feed(detector, [ON, ACC], start=10) == [10.0]


def test_on_held_like_driving_does_not_count() -> None:
    states = [ACC] + [ON] * int(MAX_ON_S + 2) + [ACC]
    assert _feed(KeyCycleDetector(), states) == []


@pytest.mark.parametrize("untrusted", ["replay", "passive", "unknown"])
def test_untrusted_state_breaks_the_cycle(untrusted: str) -> None:
    assert _feed(KeyCycleDetector(), [ACC, ON, untrusted, ACC]) == []
    assert _feed(KeyCycleDetector(), [ACC, untrusted, ON, ACC]) == []


# --- the coordinator ------------------------------------------------------


def _collect(car: Car, req_id: str, secret: str) -> dict:
    status = car.pairing.status(req_id, secret)
    assert status is not None
    return status


def test_one_request_one_cycle_pairs(car: Car) -> None:
    car.hold(ACC)
    req = car.pairing.request("Android phone")
    assert _collect(car, req.id, req.secret)["step"] == "turn_on"
    car.hold(ON)
    assert _collect(car, req.id, req.secret)["step"] == "back_to_acc"
    car.hold(ACC)
    done = _collect(car, req.id, req.secret)
    assert done["status"] == "approved"
    assert car.devices.authenticate(done["token"]) is not None
    assert done["device"]["method"] == "key-cycle"
    # Collected once: the request is gone and the token is not handed out again.
    assert car.pairing.status(req.id, req.secret) is None


def test_request_while_on_must_go_to_acc_first(car: Car) -> None:
    car.hold(ACC)
    car.hold(ON)
    req = car.pairing.request("Android phone")
    assert _collect(car, req.id, req.secret)["step"] == "set_acc"
    car.hold(ACC)  # completes a cycle that began before the request
    assert _collect(car, req.id, req.secret)["status"] == "waiting"
    car.cycle()
    assert _collect(car, req.id, req.secret)["status"] == "approved"


def test_two_requests_at_once_pair_neither(car: Car) -> None:
    car.hold(ACC)
    owner = car.pairing.request("Android phone")
    stranger = car.pairing.request("Galaxy S23")
    car.cycle()
    for req in (owner, stranger):
        result = _collect(car, req.id, req.secret)
        assert result["status"] == "conflict"
        assert "Android phone" in result["detail"] and "Galaxy S23" in result["detail"]
        assert "token" not in result
    assert car.devices.list() == []
    assert "pairing.conflict" in car.events


def test_a_request_during_the_owners_cycle_is_a_conflict(car: Car) -> None:
    car.hold(ACC)
    owner = car.pairing.request("Android phone")
    car.hold(ON)
    stranger = car.pairing.request("Galaxy S23")  # timed into the ON phase
    car.hold(ACC)
    assert _collect(car, owner.id, owner.secret)["status"] == "conflict"
    assert _collect(car, stranger.id, stranger.secret)["status"] == "conflict"
    assert car.devices.list() == []


def test_a_cycle_with_no_request_opens_ble_setup(car: Car) -> None:
    car.cycle()
    assert read_window_remaining(car.window) > 0
    assert "provisioning.window_opened" in car.events


def test_a_cycle_that_pairs_does_not_open_ble_setup(car: Car) -> None:
    car.hold(ACC)
    car.pairing.request("Android phone")
    car.cycle()
    assert read_window_remaining(car.window) == 0


def test_replay_cannot_pair(car: Car) -> None:
    car.hold(ACC)
    req = car.pairing.request("Android phone")
    car.hold("replay")
    status = _collect(car, req.id, req.secret)
    assert status["step"] == "unavailable"
    assert "replay" in status["detail"].lower()
    car.hold(ON)
    car.hold("replay")
    car.hold(ACC)
    assert _collect(car, req.id, req.secret)["status"] == "waiting"


def test_no_live_data_is_unavailable(car: Car) -> None:
    car.hold(None)
    req = car.pairing.request("Android phone")
    car.hold(None)
    assert _collect(car, req.id, req.secret)["step"] == "unavailable"


def test_requests_expire(car: Car) -> None:
    car.hold(ACC)
    req = car.pairing.request("Android phone")
    car.hold(ACC, REQUEST_TTL_S + 2)
    assert _collect(car, req.id, req.secret)["status"] == "expired"
    car.cycle()
    assert car.devices.list() == []


def test_a_request_needs_its_secret(car: Car) -> None:
    req = car.pairing.request("Android phone")
    assert car.pairing.status(req.id, "wrong") is None
    assert not car.pairing.cancel(req.id, "wrong")
    assert car.pairing.cancel(req.id, req.secret)
    assert car.pairing.status(req.id, req.secret) is None


def test_waiting_requests_are_capped(car: Car) -> None:
    for i in range(MAX_PENDING):
        car.pairing.request(f"device {i}")
    with pytest.raises(OverflowError):
        car.pairing.request("one more")


def test_an_approval_survives_a_failed_save(car: Car, monkeypatch: pytest.MonkeyPatch) -> None:
    car.hold(ACC)
    req = car.pairing.request("Android phone")
    car.cycle()

    def full(*_: object) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr("src.control.paired_devices._durable_write", full)
    with pytest.raises(OSError):
        car.pairing.status(req.id, req.secret)
    monkeypatch.undo()
    assert "token" in _collect(car, req.id, req.secret)


# --- approval by a trusted caller -----------------------------------------


def test_pending_lists_waiting_requests_oldest_first_without_secrets(car: Car) -> None:
    first = car.pairing.request("Android phone")
    car.hold(ACC, 3)
    second = car.pairing.request("Laptop")
    pending = car.pairing.pending()
    assert [p.id for p in pending] == [first.id, second.id]
    assert pending[0].age_s == pytest.approx(3.0)
    assert pending[1].expires_in_s == pytest.approx(REQUEST_TTL_S)
    public = pending[0].public()
    assert set(public) == {"id", "name", "age_s", "expires_in_s"}
    assert first.secret not in str(public)


def test_approve_picks_one_of_several_and_records_who(car: Car) -> None:
    owner = car.pairing.request("Android phone")
    laptop = car.pairing.request("Laptop")
    assert car.pairing.approve(laptop.id, by="a1b2c3")
    done = _collect(car, laptop.id, laptop.secret)
    assert done["status"] == "approved"
    assert done["device"]["method"] == "approved-by:a1b2c3"
    assert "a1b2c3" in done["detail"]
    assert car.devices.authenticate(done["token"]) is not None
    # The other request is untouched: still waiting, still listed.
    assert _collect(car, owner.id, owner.secret)["status"] == "waiting"
    assert [p.id for p in car.pairing.pending()] == [owner.id]
    assert "pairing.approved" in car.events


def test_approve_only_a_waiting_request(car: Car) -> None:
    req = car.pairing.request("Android phone")
    assert car.pairing.approve(req.id, by="shared-token")
    assert not car.pairing.approve(req.id, by="shared-token")  # already approved
    with pytest.raises(KeyError):
        car.pairing.approve("no-such-request", by="shared-token")
    stale = car.pairing.request("Old phone")
    car.hold(ACC, REQUEST_TTL_S + 2)
    assert not car.pairing.approve(stale.id, by="shared-token")  # expired
    assert car.pairing.pending() == []


def test_approval_cannot_rescue_a_conflict(car: Car) -> None:
    car.hold(ACC)
    owner = car.pairing.request("Android phone")
    car.pairing.request("Galaxy S23")
    car.cycle()
    assert not car.pairing.approve(owner.id, by="shared-token")
    assert car.devices.list() == []


def test_a_key_cycle_approval_is_announced(car: Car) -> None:
    car.hold(ACC)
    car.pairing.request("Android phone")
    car.cycle()
    assert "pairing.approved" in car.events


def test_every_phase_change_is_logged_with_its_state_and_on_time(
    car: Car, caplog: pytest.LogCaptureFixture
) -> None:
    """The truck check (phone-ui P2.4) reads the journal: when the key went
    back to ACC is the operator's note, when the Pi saw it is this line."""
    caplog.set_level(logging.INFO, logger="src.control.key_cycle_pairing")
    car.hold(ACC)
    car.hold(ON, 5)
    car.hold(ACC)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("key phase")]
    assert lines == [
        "key phase unknown -> acc (vehicle_state engine_off)",
        "key phase acc -> on (vehicle_state ignition_only)",
        "key phase on -> acc (vehicle_state engine_off) after 5.0 s ON",
    ]
