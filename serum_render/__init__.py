"""
serum-render — batch Serum 1 (.fxp) and Serum 2 (.SerumPreset) preset
rendering via DawDreamer.

Public API:
    from serum_render import (
        RenderConfig,
        Renderer,
        ParallelRenderer,
        render_preset,
    )

RenderConfig is eager (pure-Python). The renderer entry points are
exposed lazily via PEP 562 __getattr__ so that importing worker-side
modules inside a loky worker process does NOT transitively import
dawdreamer / numpy at module level — dawdreamer must be the first
non-stdlib import in a render process, enforced inside EngineHost.
"""
from __future__ import annotations

from .config import RenderConfig

__all__ = [
    "RenderConfig",
    "Renderer",
    "ParallelRenderer",
    "render_preset",
]


def __getattr__(name: str):
    if name in ("Renderer", "ParallelRenderer", "render_preset"):
        from . import api
        return getattr(api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
