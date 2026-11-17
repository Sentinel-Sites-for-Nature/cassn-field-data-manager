#!/usr/bin/env python3
"""Compatibility wrapper: the SoundHub CLI now lives in ``cassn.soundhub.cli``.

``python utils/prep_soundhub.py <command>`` keeps working for operators,
``utils/cassn-clear-soundhub``, and the commands shown in the app.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cassn.soundhub.cli import (  # noqa: E402,F401
    _print_preflight,
    _print_verification,
    cmd_clear_completed,
    cmd_stage,
    cmd_status,
    cmd_upload,
    find_deployments,
    main,
)

if __name__ == "__main__":
    sys.exit(main())
