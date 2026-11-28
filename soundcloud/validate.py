"""Validate and sanitize payloads from the page / disk cache."""
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
from .uri import (
    is_allowed_artwork_uri,
    is_playback_id,
    is_soundcloud_uri,
    normalize_list_artwork_url,
)

def _bounded_string(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        return None
    return value

def _finite_number(value: object, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number

def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return minimum
    return value if minimum <= value <= maximum else minimum

def validate_status_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    title = _bounded_string(payload.get("title", ""), MAX_TITLE_BYTES)
    artist = _bounded_string(payload.get("artist", ""), MAX_ARTIST_BYTES)
    page_url = _bounded_string(payload.get("url", ""), MAX_URL_BYTES)
    art_url = _bounded_string(payload.get("artUrl", ""), MAX_URL_BYTES)
    duration = _finite_number(payload.get("duration", 0), 0, 60 * 60 * 24 * 7)
    position = _finite_number(payload.get("position", 0), 0, 60 * 60 * 24 * 7)
    error = _bounded_string(payload.get("error", ""), 128)
    player_present = payload.get("playerPresent", True)
    playback_state = payload.get("playbackState")
    if playback_state is None:
        playback_state = "playing" if payload.get("playing") is True else (
            "paused" if player_present is True else "idle"
        )
    if (None in (title, artist, page_url, art_url, duration, position, error)
            or not isinstance(player_present, bool)
            or playback_state not in {"idle", "resolving", "buffering", "playing", "paused", "error"}):
        return None
    if page_url and not is_soundcloud_uri(page_url):
        return None
    if art_url and not is_allowed_artwork_uri(art_url):
        art_url = ""
    return {
        "playerPresent": player_present,
        "playbackState": playback_state,
        "playing": payload.get("playing") is True,
        "playbackId": payload.get("playbackId") if is_playback_id(payload.get("playbackId")) else "",
        "title": title,
        "artist": artist,
        "artUrl": art_url,
        "loggedIn": payload.get("loggedIn") is True,
        "url": page_url,
        "duration": duration,
        "position": min(position, duration) if duration else position,
        "waveform": validate_waveform(payload.get("waveform", [])),
        **({"error": error} if error else {}),
    }

def validate_waveform(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) > MAX_WAVEFORM_BARS:
        return []
    if not all(
        isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 100
        for level in value
    ):
        return []
    return list(value)

def validate_tracks_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("tracks"), list):
        return None
    incoming = payload["tracks"]
    if len(incoming) > MAX_TRACKS:
        return None
    tracks = []
    for item in incoming:
        if not isinstance(item, dict):
            return None
        title = _bounded_string(item.get("title", ""), MAX_TITLE_BYTES)
        artist = _bounded_string(item.get("artist", ""), MAX_ARTIST_BYTES)
        track_url = _bounded_string(item.get("url", ""), MAX_URL_BYTES)
        art_url = _bounded_string(item.get("artUrl", ""), MAX_URL_BYTES)
        playback_id = _bounded_string(item.get("playbackId", ""), 64)
        if (None in (title, artist, track_url, art_url, playback_id)
                or not title or not isinstance(track_url, str) or not is_soundcloud_uri(track_url)
                or (playback_id and not is_playback_id(playback_id))):
            return None
        if art_url and not is_allowed_artwork_uri(art_url):
            art_url = ""
        tracks.append(
            {
                "playbackId": playback_id,
                "title": title,
                "artist": artist,
                "url": track_url,
                "artUrl": art_url,
                "playCount": _bounded_integer(item.get("playCount"), 0, MAX_PLAY_COUNT),
                "durationMs": _bounded_integer(
                    item.get("durationMs"), 0, MAX_TRACK_DURATION_MS
                ),
            }
        )
    return {"tracks": tracks}

def sanitize_track_cache(payload: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(payload, dict):
        return {}
    sanitized = {}
    for source in ("home", "feed"):
        tracks = payload.get(source)
        if not isinstance(tracks, list) or not tracks:
            continue
        result = validate_tracks_payload({"tracks": tracks})
        validated_tracks = result.get("tracks") if result is not None else None
        if isinstance(validated_tracks, list) and validated_tracks:
            sanitized[source] = validated_tracks
    return sanitized

def validate_collections_payload(payload: object) -> list[dict[str, object]] | None:
    items = payload.get("collections") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) > 50:
        return None
    collections = []
    for item in items:
        if not isinstance(item, dict):
            return None
        kind = item.get("kind")
        identifier = item.get("id")
        title = _bounded_string(item.get("title", ""), MAX_TITLE_BYTES)
        subtitle = _bounded_string(item.get("subtitle", ""), MAX_ARTIST_BYTES)
        url = _bounded_string(item.get("url", ""), MAX_URL_BYTES)
        art_url = _bounded_string(item.get("artUrl", ""), MAX_URL_BYTES)
        if (kind not in {"playlist", "user", "mix"} or isinstance(identifier, bool)
                or not isinstance(identifier, int) or not 0 < identifier < 10**16
                or None in (title, subtitle, url, art_url) or not title
                or not is_soundcloud_uri(url)):
            return None
        collections.append({
            "kind": kind,
            "id": identifier,
            "title": title,
            "subtitle": subtitle,
            "url": url,
            "artUrl": art_url if art_url and is_allowed_artwork_uri(art_url) else "",
        })
    return collections

def sanitize_player_state(payload: object) -> dict[str, object]:
    """The remembered volume, last track, and its queue, all re-validated."""
    if not isinstance(payload, dict):
        return {}
    state: dict[str, object] = {}
    volume = _finite_number(payload.get("volume"), 0, 1)
    if volume is not None:
        state["volume"] = volume
    track_result = validate_tracks_payload({"tracks": [payload.get("track")]}) if payload.get("track") else None
    if track_result and is_playback_id(track_result["tracks"][0].get("playbackId")):
        state["track"] = track_result["tracks"][0]
        state["position"] = _finite_number(payload.get("position", 0), 0, 60 * 60 * 24 * 7) or 0.0
        queue_result = validate_tracks_payload({"tracks": payload.get("queue", [])})
        queue = [
            track for track in (queue_result["tracks"] if queue_result else [])
            if is_playback_id(track.get("playbackId"))
        ][:MAX_QUEUE]
        source = payload.get("queueSource")
        index = payload.get("queueIndex")
        if (queue and source in QUEUE_SOURCES and isinstance(index, int)
                and not isinstance(index, bool) and 0 <= index < len(queue)
                and queue[index]["playbackId"] == state["track"]["playbackId"]):
            state["queue"], state["queueSource"], state["queueIndex"] = queue, source, index
    return state
