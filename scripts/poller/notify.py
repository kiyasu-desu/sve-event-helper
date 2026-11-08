"""Compatibility import; use app/client/core/notification_sender.py."""
import sys
from pathlib import Path
if __name__ == "__main__":
    raise SystemExit("Use deploy/diagnostics.py --test-notify for an explicit notification test")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app/client/core"))
import notification_sender
sys.modules[__name__] = notification_sender
