"""The base every schema that refuses unknown keys derives from.

``class _Strict(BaseModel): model_config = ConfigDict(extra="forbid")`` was
written out once per module (51 copies by R24.2, two of them with ``frozen``
and the rest without), so a new schema's author chose the config by copying
whichever neighbour they happened to read. Now there are two bases and the
choice is the class name:

* :class:`StrictModel` -- ``extra="forbid"``: a mistyped or retired key in a
  YAML file, a request body or a stored record is a validation error, never a
  silently dropped field.
* :class:`FrozenStrictModel` -- the same, and instances are immutable and
  hashable-by-value (``frozen=True``).

A model that needs a further key (``populate_by_name``,
``protected_namespaces``, ``strict``) sets it in its own ``model_config``;
pydantic merges that over the base's, so ``extra="forbid"`` is never lost by
accident.

A foundation module (layer 0): it imports nothing from ``src``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """A schema that forbids extra keys."""

    model_config = ConfigDict(extra="forbid")


class FrozenStrictModel(StrictModel):
    """A schema that forbids extra keys and is immutable."""

    model_config = ConfigDict(frozen=True)
