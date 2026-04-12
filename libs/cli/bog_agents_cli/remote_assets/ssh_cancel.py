"""Cancel an SSH sandbox task by killing its process group and writing status."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

status_path = Path(sys.argv[1]).expanduser()
if not status_path.exists():
    sys.stdout.write(json.dumps({"cancelled": False, "reason": "missing-status-file"}))
    raise SystemExit(0)

data = json.loads(status_path.read_text(encoding="utf-8"))
pid = int(data.get("pid", 0) or 0)
cancelled = False
if pid:
    killpg = getattr(os, "killpg", None)
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        if killpg is None:
            break
        try:
            killpg(pid, sig)
            cancelled = True
            if sig == signal.SIGTERM:
                time.sleep(0.2)
        except ProcessLookupError:
            cancelled = True
            break
        except OSError:
            break

data["status"] = "cancelled"
data["completed_at"] = time.time()
data["cancelled"] = cancelled
status_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
sys.stdout.write(json.dumps(data))
