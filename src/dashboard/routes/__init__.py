"""Dashboard route registration modules."""

from .control import register_control_routes
from .public import register_public_routes
from .streams import register_stream_routes

__all__ = [
    "register_control_routes",
    "register_public_routes",
    "register_stream_routes",
]
