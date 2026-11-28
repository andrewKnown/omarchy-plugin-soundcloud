"""MPRIS property helpers for media-key integration."""
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
from .uri import is_allowed_artwork_uri, is_playback_id, is_soundcloud_uri

def mpris_player_properties(state: dict[str, object], art_url: str, volume: float) -> dict[str, tuple[str, object]]:
    """MPRIS Player properties (signature, value) for a validated status."""
    title = str(state.get("title") or "")
    playing = state.get("playing") is True
    duration = float(state.get("duration") or 0)
    playback_id = str(state.get("playbackId") or "")
    track_number = playback_id.rpartition(":")[2] if is_playback_id(playback_id) else "none"
    metadata: dict[str, tuple[str, object]] = {
        "mpris:trackid": ("o", f"{MPRIS_PATH}/track/{track_number}"),
    }
    if title:
        metadata["xesam:title"] = ("s", title)
        artist = str(state.get("artist") or "")
        if artist:
            metadata["xesam:artist"] = ("as", [artist])
        if duration > 0:
            metadata["mpris:length"] = ("x", int(duration * 1_000_000))
        if art_url and is_allowed_artwork_uri(art_url):
            metadata["mpris:artUrl"] = ("s", art_url)
    return {
        "PlaybackStatus": ("s", "Playing" if playing else ("Paused" if title else "Stopped")),
        "Rate": ("d", 1.0),
        "MinimumRate": ("d", 1.0),
        "MaximumRate": ("d", 1.0),
        "Metadata": ("a{sv}", metadata),
        "Volume": ("d", float(volume)),
        "CanGoNext": ("b", bool(title)),
        "CanGoPrevious": ("b", bool(title)),
        "CanPlay": ("b", True),
        "CanPause": ("b", True),
        "CanSeek": ("b", duration > 0),
        "CanControl": ("b", True),
    }

class _NullConnection:
    """Response sink for commands the backend issues to itself (MPRIS)."""

    def send(self, _payload: object) -> bool:
        return True

    def close(self) -> None:
        pass
