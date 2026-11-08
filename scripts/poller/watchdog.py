"""Compatibility for existing watchdog units: --agent is required."""
import sys
from pathlib import Path
if __name__ == "__main__" and "--agent" not in sys.argv[1:]:
    raise SystemExit("Legacy poller watchdog retired; use scripts/runtime/watchdog.py")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.runtime import watchdog as _watchdog
if __name__ == "__main__":
    raise SystemExit(_watchdog.main())
sys.modules[__name__] = _watchdog
