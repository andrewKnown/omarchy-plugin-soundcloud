#!/usr/bin/env python3
"""Hidden SoundCloud session/playback backend for an Omarchy bar plugin.

The only normal window is the one-time sign-in surface. Closing it hides the
WebKit view while preserving the session and audio. The bar talks to this
process over a user-only Unix socket.

Implementation lives in the ``soundcloud`` package; this module is the stable
entry point for the bar widget (``python3 -I soundcloud_app.py …``) and for
tests that load symbols from ``soundcloud_app``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from soundcloud.backend import *  # noqa: E402, F403
from soundcloud.backend import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
