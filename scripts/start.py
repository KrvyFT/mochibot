"""MochiBot launcher with automatic restart support.

Usage:
    python scripts/start.py

When Mochi requests a restart or an official release update, this wrapper
performs the process-level work and relaunches it.

For Docker or systemd deployments, use ``python -m mochi.main`` directly —
those environments already handle process restarts.
"""

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Must match mochi.shutdown.RESTART_EXIT_CODE (mochi/shutdown.py:10)
_RESTART_EXIT_CODE = 42
_UPDATE_EXIT_CODE = 44


def main():
    os.environ["MOCHIBOT_UPDATE_LAUNCHER"] = "1"
    open_browser = "--open-browser" in sys.argv[1:]
    while True:
        if open_browser:
            timer = threading.Timer(
                2.0,
                webbrowser.open,
                args=("http://127.0.0.1:8080",),
            )
            timer.daemon = True
            timer.start()
            open_browser = False
        result = subprocess.run(
            [sys.executable, "-m", "mochi.main"],
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == _RESTART_EXIT_CODE:
            print()
            print("  [start.py] Restart requested — restarting in 2s...")
            print()
            time.sleep(2)
            continue
        if result.returncode == _UPDATE_EXIT_CODE:
            from mochi.update_service import apply_pending_update

            print()
            print("  [start.py] Applying official MochiBot update...")
            update_result = apply_pending_update(sys.executable)
            if update_result:
                print(f"  [start.py] {update_result['message']}")
            print()
            time.sleep(2)
            continue
        sys.exit(result.returncode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
