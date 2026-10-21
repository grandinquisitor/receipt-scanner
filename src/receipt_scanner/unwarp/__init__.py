"""Pluggable unwarpers: flatten a roughly-cropped, possibly curved or crumpled receipt.

Pick one with RECEIPT_UNWARPER (default "uvdoc"; "none" disables). To add a backend, write a class with
`name`, `prepare()` and `unwarp(grey) -> grey`, and register it below. `unwarp` may raise; callers fall
back to the plain crop.

`prepare()` runs once in the web server (in a background thread) to fetch anything slow, such as model
files. `unwarp()` runs in a short-lived image-processing process, so it should load its model itself and
the module shouldn't import heavy libraries at the top level (the server imports it too).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import numpy as np

__all__ = ["Unwarper", "UnwarpUnavailable", "get_unwarper", "REGISTRY"]


class UnwarpUnavailable(Exception):
    """The unwarper can't run right now (e.g. its model is still downloading)."""


class Unwarper(Protocol):
    name: str

    def prepare(self) -> None:
        """Fetch whatever unwarp() will need (may be slow; called from a background thread)."""
        ...

    def unwarp(self, grey: np.ndarray) -> np.ndarray:
        """Return a flattened version of a greyscale receipt crop."""
        ...


def _uvdoc(model_dir: Path) -> Unwarper:
    from .uvdoc import UVDocUnwarper
    return UVDocUnwarper(model_dir)


REGISTRY: dict[str, Callable[[Path], Unwarper]] = {
    "uvdoc": _uvdoc,
}


def get_unwarper(model_dir: Path, name: str | None = None) -> Unwarper | None:
    """Return the configured unwarper, or None if unwarping is disabled."""
    name = (name or os.environ.get("RECEIPT_UNWARPER", "uvdoc")).lower()
    if name in ("none", "off", ""):
        return None
    try:
        return REGISTRY[name](model_dir)
    except KeyError:
        raise ValueError(f"Unknown unwarper {name!r}; choose from {sorted(REGISTRY) + ['none']}") from None
