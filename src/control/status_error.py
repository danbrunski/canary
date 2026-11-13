"""A refusal the HTTP route answers with: ``status`` and a JSON ``detail``.

The workbench, the brain's reads and jobs, and the capture session each refuse a
request with an exception carrying the status code the route answers with and
the body. Four classes held the same two fields; every route that called one
re-raised it as ``HTTPException(exc.status, exc.detail)`` by hand, and a route
that forgot answered 500 instead of the named refusal. ``create_app`` now
registers one handler for :class:`StatusError`
(:mod:`src.dashboard.status_errors`), so no route has to remember.

**The lowest layer that needs it is this one.** Every subclass is a ``control``
refusal; the handler is the dashboard's. Nothing below ``control`` answers an
HTTP request.

**Relation to :class:`~src.refusal.ReasonedRefusal`.** Siblings, not parent and
child. A ``ReasonedRefusal`` is a domain refusal with a required stable
``reason`` code and no idea it will cross HTTP; a ``StatusError`` is an HTTP
answer, and not every one has a code (a capture's refusal is a sentence, and
only some bodies carry an ``error`` key). Forcing a ``reason`` onto the second
would invent codes; forcing a ``status`` onto the first would make the
simulator know a status code. The route's translation of one into the other
(``HTTPException(409, {"error": exc.reason, "message": exc.detail})``) is a
real decision a route makes, and stays in the route.

Both pickle through :func:`src.refusal.rebuild_exception`, so a ``StatusError``
raised in the workbench's spawn child arrives with its status and body.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from src.refusal import rebuild_exception

#: What a subclass's body is: a dict (``{"error": ..., "message": ...}``), a
#: sentence, or either.
Detail = TypeVar("Detail", bound="str | dict[str, Any]")


class StatusError(Exception, Generic[Detail]):
    """A refusal that is an HTTP answer. ``status`` is the code the route
    answers with; ``detail`` its body, which the app handler puts under the
    JSON key ``"detail"``, as ``HTTPException`` does."""

    status: int
    detail: Detail

    def __init__(self, status: int, detail: Detail) -> None:
        super().__init__(self.render(detail))
        self.status = status
        self.detail = detail

    @staticmethod
    def render(detail: Any) -> str:
        """What ``str(exc)`` reads: the sentence, or a dict body's ``message``."""
        if isinstance(detail, str):
            return detail
        return str(detail.get("message", detail))

    def __reduce__(self) -> tuple[Any, ...]:
        return (rebuild_exception, (type(self), self.args), self.__dict__)


__all__ = ["StatusError"]
