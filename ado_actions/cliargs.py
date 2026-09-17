"""Argument container shared by the CLI layer and the command handlers."""
from __future__ import annotations

from types import SimpleNamespace


class Args(SimpleNamespace):
    """Attribute bag holding the options a ``cmd_*`` handler was invoked with.

    Click callbacks collect their parameters into one of these so the handlers
    stay plain functions taking a single namespace.
    """
