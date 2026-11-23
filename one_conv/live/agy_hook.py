"""agy lifecycle hook entry point: `python3 -I agy_hook.py` with the hook payload on stdin.

Run by path, without uv, so it starts fast enough for agy's per-model-call `PreInvocation` hook.
"""

import sys
from pathlib import Path

# `-I` leaves the checkout off sys.path; the package lives two levels up.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from one_conv.live import agy  # noqa: E402

if __name__ == "__main__":
    agy.main()
