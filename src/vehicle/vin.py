"""VIN validation: ISO 3779 shape and the North American check digit.

A VIN keys VIN -> profile resolution (:mod:`src.vehicle.profiles`), so a
typed or read VIN is checked before it reaches the registry (operator,
2026-10-10). Three facts:

* **Shape.** Exactly 17 characters, digits and capital letters, never
  ``I``, ``O`` or ``Q`` (ISO 3779; they read as 1 and 0). Anything else is
  refused: it is not a VIN.
* **Check digit.** Position 9 is a check digit under the North American
  scheme (49 CFR 565.15): transliterate, weight, sum modulo 11, ``10`` reads
  ``X``. A North American VIN (world manufacturer identifier starting ``1``-
  ``5``) whose check digit is wrong is refused, because it was mistyped or
  misread.
* **Elsewhere the check digit is optional.** Europe and Japan do not require
  it, so a mismatch on a VIN from outside North America is a *warning*, not a
  refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

VIN_LENGTH = 17
#: ISO 3779 excludes these three: they read as the digits 1 and 0.
FORBIDDEN_LETTERS = frozenset("IOQ")
#: The check digit's position, 1-based.
CHECK_DIGIT_POSITION = 9
#: First characters of North American world manufacturer identifiers (US 1, 4,
#: 5; Canada 2; Mexico 3), where the check digit is mandatory.
NORTH_AMERICAN_REGIONS = frozenset("12345")

_TRANSLITERATION: dict[str, int] = {
    **{str(d): d for d in range(10)},
    **dict(zip("ABCDEFGH", range(1, 9), strict=True)),
    **dict(zip("JKLMN", range(1, 6), strict=True)),
    "P": 7,
    "R": 9,
    **dict(zip("STUVWXYZ", range(2, 10), strict=True)),
}
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

CheckDigit = Literal["pass", "fail"]


class InvalidVinError(ValueError):
    """Not a usable VIN; the message says which rule it broke."""


@dataclass(frozen=True)
class VinCheck:
    """A VIN that passed the shape check, and what its check digit says."""

    vin: str
    check_digit: CheckDigit
    #: The digit position 9 should hold.
    expected_check_digit: str
    north_american: bool
    #: Set when the check digit fails on a VIN from outside North America.
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "vin": self.vin,
            "check_digit": self.check_digit,
            "expected_check_digit": self.expected_check_digit,
            "north_american": self.north_american,
            "warning": self.warning,
        }


def normalize_vin(text: str) -> str:
    """Upper-case and strip surrounding whitespace; nothing else is changed."""
    return text.strip().upper()


def expected_check_digit(vin: str) -> str:
    """The check digit for a 17-character VIN of valid characters."""
    total = sum(_TRANSLITERATION[ch] * w for ch, w in zip(vin, _WEIGHTS, strict=True))
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def _shape_problem(vin: str) -> str | None:
    if len(vin) != VIN_LENGTH:
        return f"a VIN has {VIN_LENGTH} characters; {vin!r} has {len(vin)}"
    bad = sorted({ch for ch in vin if not (ch.isascii() and ch.isalnum())})
    if bad:
        return f"a VIN holds only letters and digits; {vin!r} contains {''.join(bad)!r}"
    forbidden = sorted(FORBIDDEN_LETTERS & set(vin))
    if forbidden:
        return (
            f"a VIN never contains I, O or Q (they read as 1 and 0); {vin!r} contains "
            f"{', '.join(forbidden)}"
        )
    return None


def inspect_vin(text: str) -> tuple[VinCheck | None, str | None]:
    """``(check, None)`` for a well-formed VIN, else ``(None, why)``.

    Never raises. A check-digit failure is reported in the result; whether
    it refuses is :func:`validate_vin`'s decision.
    """
    vin = normalize_vin(text)
    problem = _shape_problem(vin)
    if problem is not None:
        return None, problem
    expected = expected_check_digit(vin)
    actual = vin[CHECK_DIGIT_POSITION - 1]
    north_american = vin[0] in NORTH_AMERICAN_REGIONS
    passed = actual == expected
    warning = None
    if not passed and not north_american:
        warning = (
            f"check digit (position {CHECK_DIGIT_POSITION}) is {actual!r}, the North American "
            f"scheme gives {expected!r}; a VIN from outside North America need not carry one, "
            "so check it against the vehicle's plate"
        )
    return (
        VinCheck(
            vin=vin,
            check_digit="pass" if passed else "fail",
            expected_check_digit=expected,
            north_american=north_american,
            warning=warning,
        ),
        None,
    )


def validate_vin(text: str) -> VinCheck:
    """The checked VIN, or :class:`InvalidVinError` naming the rule it broke.

    Refuses a malformed VIN and a North American VIN with a wrong check
    digit; a non-North-American mismatch passes with ``warning`` set.
    """
    check, problem = inspect_vin(text)
    if check is None:
        raise InvalidVinError(problem)
    if check.check_digit == "fail" and check.north_american:
        raise InvalidVinError(
            f"check digit mismatch: position {CHECK_DIGIT_POSITION} of {check.vin!r} is "
            f"{check.vin[CHECK_DIGIT_POSITION - 1]!r}, its other characters give "
            f"{check.expected_check_digit!r}; a North American VIN always carries a valid "
            "check digit, so this one was mistyped or misread"
        )
    return check


__all__ = [
    "CHECK_DIGIT_POSITION",
    "VIN_LENGTH",
    "InvalidVinError",
    "VinCheck",
    "expected_check_digit",
    "inspect_vin",
    "normalize_vin",
    "validate_vin",
]
