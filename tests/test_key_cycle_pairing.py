"""Pairing by key cycle (src/control/key_cycle_pairing.py).

ACC -> ON -> ACC, read from the classified vehicle state: ON is the ECM
answering OBD (``ignition_only`` / ``engine_running``); anything else live is
ACC. The car is the proof, so the interesting cases are the ones where it
must *not* approve: two requests at once, a request made after the key was
already ON, ON held like driving, and states that are not the car at all.
"""

from __future__ import annotations

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
