"""SoundCloud HTTP likes API and bot-check error."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import pwd
import re
import secrets
import socket
import sqlite3
import ssl
import stat
import struct
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, cast
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .backend_constants import *  # noqa: F403
from .commands import oauth_token_from_cookies
from .media import soundcloud_api_request
from .paths import log_event
from .uri import is_playback_id

class BotCheckRequired(OSError):
    """SoundCloud's bot protection answered with a captcha instead of the API."""

class TrackLikes:
    """The signed-in account's liked tracks, read over the private API.

    Writes go through `write` because SoundCloud's bot protection rejects
    like/unlike requests that do not come from its own web page.
    """

    RETRY_SECONDS = 60.0
    MAX_LIKE_PAGES = 50

    def __init__(self, load_token, write, request=soundcloud_api_request, spawn=None):
        self.load_token = load_token
        self.write = write
        self.request = request
        self.spawn = spawn or (lambda target: threading.Thread(target=target, daemon=True).start())
        self.lock = threading.Lock()
        self.token: str | None = None
        self.user_id: int | None = None
        self.liked_ids: set[int] | None = None
        self.loading = False
        self.last_attempt = -math.inf

    def _session(self) -> str:
        token = self.load_token()
        if not token:
            raise PermissionError("not signed in")
        if token != self.token:
            self.token, self.user_id, self.liked_ids = token, None, None
        if self.user_id is None:
            me = self.request("GET", "/me", token)
            user_id = me.get("id") if isinstance(me, dict) else None
            if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
                raise ValueError("invalid account response")
            self.user_id = user_id
        if self.liked_ids is None:
            liked_ids = set()
            path = "/me/track_likes/ids?limit=200"
            # SoundCloud pages these 200 at a time; stop at 10,000 likes.
            for _page in range(self.MAX_LIKE_PAGES):
                payload = self.request("GET", path, token, 1024 * 1024)
                ids = payload.get("collection") if isinstance(payload, dict) else None
                if not isinstance(ids, list):
                    raise ValueError("invalid likes response")
                liked_ids.update(
                    value for value in ids
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0
                )
                next_href = payload.get("next_href")
                parsed = urlparse(next_href) if isinstance(next_href, str) else None
                if (not ids or parsed is None or parsed.scheme != "https"
                        or parsed.netloc != "api-v2.soundcloud.com"
                        or parsed.path != "/me/track_likes/ids"):
                    break
                path = parsed.path + "?" + parsed.query
            self.liked_ids = liked_ids
        return token

    def is_liked(self, track_id: int) -> bool | None:
        liked_ids = self.liked_ids
        return None if liked_ids is None else track_id in liked_ids

    def load_in_background(self, on_loaded) -> None:
        now = time.monotonic()
        if self.liked_ids is not None or self.loading or now - self.last_attempt < self.RETRY_SECONDS:
            return
        self.loading = True
        self.last_attempt = now

        def worker():
            try:
                with self.lock:
                    self._session()
                on_loaded()
            except (OSError, ValueError):
                pass
            finally:
                self.loading = False

        self.spawn(worker)

    def set_liked(self, track_id: int, liked: bool) -> None:
        with self.lock:
            token = self._session()
            self.write(cast(int, self.user_id), track_id, liked, token)
            liked_ids = cast(set[int], self.liked_ids)
            if liked:
                liked_ids.add(track_id)
            else:
                liked_ids.discard(track_id)
