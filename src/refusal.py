"""A refusal that names why, and the one rule every such exception pickles by.

Many modules refuse a request with an exception carrying a stable ``reason``
code and a human ``detail``: the simulator, the twin, the workbench's pure
functions. Each used to write its own ``__init__`` (``self.reason = reason``,
sometimes ``.message``) and its own ``__reduce__``, and the ones that forgot
the second did not survive the trip back from the workbench's spawn child
with their fields: the pool broke and the caller saw an opaque 503
(E09.2c, 2026-10-16).

:class:`ReasonedRefusal` is that class once. A subclass states only what is
its own: the builtin it must still be catchable as (``ValueError``: a refusal
raised inside a pydantic validator is turned into a validation error only if
it is one), and how ``str(exc)`` reads (:meth:`ReasonedRefusal.render`).

**Pickling.** An exception that pickles through ``cls(*self.args)`` is rebuilt
by calling its ``__init__`` with whatever ``args`` it passed to
``super().__init__``: one formatted string, where the constructor wants two
arguments. :func:`rebuild_exception` rebuilds from ``__new__`` instead and
restores ``args`` and the instance dict, so every field survives whatever the
subclass's constructor signature is (a subclass with a keyword-only extra
field needs no ``__reduce__`` of its own). ``tests/test_exceptions_pickle.py``
holds every subclass to the round trip.

This is layer 0: nothing here imports anything of the project's.
"""

from __future__ import annotations

from typing import Any


def rebuild_exception(cls: type[BaseException], args: tuple[Any, ...]) -> BaseException:
    """``cls`` with ``args``, without running its ``__init__``.

    Pickle then restores the instance dict over it (``BaseException`` applies
    the state as attributes), so the result equals the original in ``args``,
    ``str`` and every field.
    """
    return cls.__new__(cls, *args)


class ReasonedRefusal(Exception):  # noqa: N818 - named by the roadmap; every subclass is an *Error
    """A refusal with a stable ``reason`` code and a human ``detail``.

    ``str(exc)`` is ``"<reason>: <detail>"`` unless a subclass overrides
    :meth:`render`. ``message`` is ``detail`` under the name older callers use.
    """

    reason: str
    detail: str

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(self.render(reason, detail))
        self.reason = reason
        self.detail = detail

    @staticmethod
    def render(reason: str, detail: str) -> str:
        """What ``str(exc)`` reads."""
        return f"{reason}: {detail}"

    @property
    def message(self) -> str:
        return self.detail

    def __reduce__(self) -> tuple[Any, ...]:
        return (rebuild_exception, (type(self), self.args), self.__dict__)


__all__ = ["ReasonedRefusal", "rebuild_exception"]
