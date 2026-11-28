"""Compatibility facade: re-exports the SoundCloud backend package surface.

Tests and older imports load this module as ``soundcloud_app`` / ``soundcloud.backend``.
Real implementations live in the sibling modules.
"""
from __future__ import annotations

from .backend_constants import *  # noqa: F403
from .paths import *  # noqa: F403
from .uri import *  # noqa: F403
from .store import CookieStore, PrivateJsonStore
from .commands import *  # noqa: F403
from .scripts import *  # noqa: F403
from .validate import *  # noqa: F403
from .artwork import *  # noqa: F403
from .media import *  # noqa: F403
from .api import *  # noqa: F403
from .server import *  # noqa: F403
from .mpris import *  # noqa: F403
from .application import build_application
from .cli import request_backend, launch_backend, parse_args, main

# Private names tests patch on the loaded module
from .media import _media_opener, _api_opener  # noqa: F401
from .paths import _event_log_fd, _event_log_lock  # noqa: F401
from .mpris import _NullConnection  # noqa: F401
from .validate import _bounded_string, _finite_number, _bounded_integer  # noqa: F401
from .artwork import _image_kind_and_dimensions, _decode_chunked_body  # noqa: F401
from .media import _load_gi, _NoMediaRedirect, _set_media_response_timeout  # noqa: F401
