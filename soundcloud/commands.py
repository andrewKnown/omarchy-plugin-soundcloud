"""Parse bar/socket command strings and small queue helpers."""
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
from .uri import is_playback_id

def source_uri(source: str) -> str | None:
    return {
        "home": "https://soundcloud.com/discover",
        "likes": "https://soundcloud.com/you/likes",
        "feed": "https://soundcloud.com/feed",
    }.get(source)

def normalize_backend_command(command: str) -> str:
    return "show" if command == "launch" else command

def track_source_from_command(command: str) -> str | None:
    return {
        "tracks:home": "home",
        "tracks:feed": "feed",
    }.get(command)

def load_more_source_from_command(command: str) -> str | None:
    return {
        "load-more:home": "home",
        "load-more:feed": "feed",
    }.get(command)

def playback_id_from_command(command: str) -> str | None:
    if not command.startswith("play:"):
        return None
    value = command.removeprefix("play:")
    return value if is_playback_id(value) else None

def track_like_from_command(command: str) -> tuple[str, bool] | None:
    for prefix, liked in (("like:", True), ("unlike:", False)):
        if command.startswith(prefix):
            value = command.removeprefix(prefix)
            return (value, liked) if is_playback_id(value) else None
    return None

def oauth_token_from_cookies(records: list[dict[str, object]]) -> str | None:
    for record in records:
        domain = str(record.get("domain", "")).lower().lstrip(".").rstrip(".")
        value = record.get("value")
        if (record.get("name") == "oauth_token" and domain == "soundcloud.com"
                and isinstance(value, str) and re.fullmatch(r"[0-9A-Za-z._-]{1,2048}", value)):
            return value
    return None

def merge_track_pages(
    existing: list[dict[str, object]],
    incoming: list[dict[str, object]],
    *,
    reset: bool,
    limit: int = MAX_TRACKS,
) -> list[dict[str, object]]:
    merged: OrderedDict[str, dict[str, object]] = OrderedDict()
    if not reset:
        for track in existing:
            url = track.get("url") if isinstance(track, dict) else None
            if isinstance(url, str) and url:
                merged[url] = track
    for track in incoming:
        url = track.get("url") if isinstance(track, dict) else None
        if isinstance(url, str) and url:
            merged[url] = track
    return list(merged.values())[:max(0, limit)]

def search_from_command(command: str) -> tuple[str, str] | None:
    """Return (kind, query) for search:, search-playlists:, and search-users:."""
    prefix, separator, query = command.partition(":")
    if not separator or prefix not in SEARCH_KINDS:
        return None
    query = query.strip()
    if (not query or len(query.encode("utf-8")) > MAX_SEARCH_QUERY_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in query)):
        return None
    return SEARCH_KINDS[prefix], query

def search_query_from_command(command: str) -> str | None:
    found = search_from_command(command)
    return found[1] if found and command.startswith("search:") else None

def collection_from_command(command: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"open:(playlist|user|mix):([1-9][0-9]{0,15})", command)
    return (match.group(1), int(match.group(2))) if match else None

def extend_queue(queue: list[dict[str, object]], index: int,
                 extra: list[dict[str, object]]) -> tuple[list[dict[str, object]], int]:
    """Append unseen playable tracks, dropping played ones so the queue stays bounded."""
    known = {track.get("playbackId") for track in queue}
    added = []
    for track in extra:
        playback_id = track.get("playbackId")
        if is_playback_id(playback_id) and playback_id not in known:
            known.add(playback_id)
            added.append(track)
    combined = list(queue) + added
    overflow = max(0, len(combined) - MAX_QUEUE)
    overflow = min(overflow, max(0, index))
    return combined[overflow:][:MAX_QUEUE], index - overflow

def shuffle_from_command(command: str) -> str | None:
    """shuffle:<source> plays that list in a random order."""
    source = command.removeprefix("shuffle:") if command.startswith("shuffle:") else ""
    return source if source in QUEUE_SOURCES else None

def shuffled_tracks(tracks: list[dict[str, object]], current: str = "") -> list[dict[str, object]]:
    """Playable tracks in random order, never starting on the current track."""
    playable = [track for track in tracks if is_playback_id(track.get("playbackId"))][:MAX_QUEUE]
    secrets.SystemRandom().shuffle(playable)
    if len(playable) > 1 and playable[0].get("playbackId") == current:
        playable.append(playable.pop(0))
    return playable

def queued_play_from_command(command: str) -> tuple[str, str] | None:
    """play-in:<source>:<playback id> plays a track and queues its list."""
    match = re.fullmatch(r"play-in:([a-z]+):(soundcloud:tracks:[1-9][0-9]*)", command)
    if not match or match.group(1) not in QUEUE_SOURCES:
        return None
    return match.group(1), match.group(2)

def volume_from_command(command: str) -> float | None:
    if not command.startswith("volume:"):
        return None
    try:
        value = float(command.removeprefix("volume:"))
    except ValueError:
        return None
    return max(0.0, min(1.0, value)) if math.isfinite(value) else None

def queue_step(length: int, index: int, step: int) -> int | None:
    """Index of the next or previous queued track, or None at either end."""
    target = index + step
    return target if 0 <= index < length and 0 <= target < length else None

def should_auto_skip_unavailable(
    state: str,
    queue_length: int,
    consecutive_skips: int,
    page_playing: bool = False,
    limit: int = 25,
) -> bool:
    """True when a failed resolve/play should advance to the next queued track."""
    if page_playing or state != "error" or queue_length <= 0 or limit <= 0:
        return False
    return consecutive_skips < min(queue_length, limit)
