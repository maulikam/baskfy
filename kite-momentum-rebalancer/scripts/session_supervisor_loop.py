"""The ``session-supervisor`` container's entry: ``app.session_supervisor.run_forever`` (LV4).

The clock lives in the module (``next_wake``, 08:30–15:50 IST on weekdays, a tick every ten
seconds) so a test can import it; this wrapper is the compose command, matching the desk's other
loops. Nothing here places an order.
"""

from __future__ import annotations

from app.session_supervisor import main

if __name__ == "__main__":
    raise SystemExit(main())
