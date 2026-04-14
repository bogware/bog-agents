"""Read the current SSH sandbox task status file and print JSON to stdout."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

status_path = Path(sys.argv[1]).expanduser()
if not status_path.exists():
    sys.stdout.write(json.dumps({"status": "pending"}))
    raise SystemExit(0)

data = json.loads(status_path.read_text(encoding="utf-8"))
pid = int(data.get("pid", 0) or 0)
if data.get("status") in {"running", "pending"} and pid:
    try:
        os.kill(pid, 0)
    except OSError:
        if data.get("status") == "running":
            data["status"] = "failed"
            data.setdefault("error", "Remote sandbox process exited unexpectedly.")

stdout_file = Path(str(data.get("output_file", ""))).expanduser()
stderr_file = Path(str(data.get("stderr_file", ""))).expanduser()
if stdout_file.exists():
    data["output_preview"] = stdout_file.read_text(encoding="utf-8", errors="replace")[
        :4000
    ]
if stderr_file.exists():
    data["error_preview"] = stderr_file.read_text(encoding="utf-8", errors="replace")[
        :4000
    ]

sys.stdout.write(json.dumps(data))
