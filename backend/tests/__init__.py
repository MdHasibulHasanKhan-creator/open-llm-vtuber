"""Backend test-suite (pytest).

    cd backend && pytest -q

All tests are pure-python (no network, no models) so they run in CI in a second.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `app` importable when pytest is invoked from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
