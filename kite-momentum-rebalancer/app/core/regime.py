"""Moved to :mod:`baskfy_core.exposure.regime` at M15 (P3.3).

Kept as a re-export so every `from ..core.regime import ...` across the desk keeps working. The
module was already pure — no I/O, no clock, no config — which is exactly why it belonged in core.
"""

from baskfy_core.exposure.regime import *  # noqa: F401,F403
