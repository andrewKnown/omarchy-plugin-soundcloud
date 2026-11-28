"""GStreamer playback and private media / HLS fetching."""
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
from .paths import log_event
from .uri import is_soundcloud_uri
from .validate import _bounded_string

def _opener(name: str):
    """Prefer openers patched on the backend facade (tests), else this module."""
    import sys
    facade = sys.modules.get("soundcloud.backend")
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)
    return globals()[name]


def missing_gstreamer_packages(find) -> list[str]:
    return [
        package for package, elements in GSTREAMER_PACKAGE_ELEMENTS
        if not all(find(name) for name in elements)
    ]

def install_hint(packages: list[str]) -> str:
    return f"Missing {', '.join(packages)}. Run: omarchy pkg add {' '.join(packages)}"

def dependency_check() -> dict[str, object]:
    result: dict[str, object] = {
        "python_gobject": False,
        "gtk": "3.0",
        "webkit2": "4.1",
        "gstreamer_autoaudiosink": False,
        "gstreamer_aac_decoder": False,
        "gstreamer_playback_elements": False,
        "ok": False,
    }
    try:
        import gi
    except ImportError:
        result["error"] = install_hint(["python-gobject"])
        return result
    result["python_gobject"] = True
    missing_libraries = []
    for namespace, version, package in (
        ("Gtk", "3.0", "gtk3"), ("WebKit2", "4.1", "webkit2gtk-4.1"), ("Gst", "1.0", "gstreamer"),
    ):
        try:
            gi.require_version(namespace, version)
        except ValueError:
            missing_libraries.append(package)
    if missing_libraries:
        result["error"] = install_hint(missing_libraries)
        return result
    try:
        from gi.repository import Gst, Gtk, WebKit2  # noqa: F401

        Gst.init(None)
        find = lambda name: bool(Gst.ElementFactory.find(name))
        result["gstreamer_autoaudiosink"] = find("autoaudiosink")
        result["gstreamer_aac_decoder"] = find("avdec_aac")
        result["gstreamer_playback_elements"] = all(
            find(name) for name in GSTREAMER_PACKAGE_ELEMENTS[0][1]
        )
        missing_packages = missing_gstreamer_packages(find)
        if missing_packages:
            result["error"] = install_hint(missing_packages)
        result["ok"] = not missing_packages
    except (ImportError, ValueError) as exc:
        result["error"] = str(exc)
    return result

def _load_gi():
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("Gtk", "3.0")
    gi.require_version("Soup", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gio, GLib, Gst, Gtk, Soup, WebKit2

    return Gio, GLib, Gst, Gtk, Soup, WebKit2

def validate_stream_uri(uri: object) -> str | None:
    value = _bounded_string(uri, 8192)
    if value is None:
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    host = parsed.hostname.lower().rstrip(".")
    if not (host.endswith(".sndcdn.com") or host.endswith(".soundcloud.cloud")):
        return None
    return value

class _NoMediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None

def _set_media_response_timeout(response, timeout: float) -> None:
    candidates = (
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
    )
    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "settimeout"):
            candidate.settimeout(timeout)
            return

def fetch_private_media(
    uri: str,
    maximum: int,
    *,
    deadline: float | None = None,
    cancelled=lambda: False,
) -> bytes:
    if cancelled():
        raise concurrent.futures.CancelledError
    remaining = 15.0 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("media operation deadline exceeded")
    validated = validate_stream_uri(uri)
    if validated is None:
        raise ValueError("invalid media URL")
    request = Request(validated, headers={"User-Agent": "Mozilla/5.0"})
    with _opener("_media_opener").open(request, timeout=min(15.0, remaining)) as response:
        if cancelled():
            raise concurrent.futures.CancelledError
        if validate_stream_uri(response.geturl()) is None:
            raise ValueError("invalid media response URL")
        length = response.headers.get("Content-Length")
        if length and int(length) > maximum:
            raise ValueError("media response too large")
        data = bytearray()
        while len(data) <= maximum:
            if cancelled():
                raise concurrent.futures.CancelledError
            remaining = 15.0 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("media operation deadline exceeded")
            _set_media_response_timeout(response, min(15.0, remaining))
            read = getattr(response, "read1", None)
            chunk_size = min(64 * 1024, maximum + 1 - len(data))
            part = cast(
                bytes,
                read(chunk_size) if callable(read) else response.read(min(1, chunk_size)),
            )
            if not part:
                break
            data.extend(part)
    if cancelled():
        raise concurrent.futures.CancelledError
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("media operation deadline exceeded")
    if len(data) > maximum:
        raise ValueError("media response too large")
    return bytes(data)

def hls_segment_urls(
    uri: str,
    depth: int = 0,
    *,
    deadline: float | None = None,
    cancelled=lambda: False,
) -> list[str]:
    if depth > 2:
        raise ValueError("nested HLS playlist")
    text = fetch_private_media(
        uri, 512 * 1024, deadline=deadline, cancelled=cancelled
    ).decode("utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    media_lines = [line for line in lines if not line.startswith("#")]
    if not media_lines:
        raise ValueError("empty HLS playlist")
    resolved = [urljoin(uri, line) for line in media_lines]
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        return hls_segment_urls(
            resolved[0], depth + 1, deadline=deadline, cancelled=cancelled
        )
    if len(resolved) > 4096 or any(validate_stream_uri(value) is None for value in resolved):
        raise ValueError("invalid HLS segments")
    return resolved

def soundcloud_api_request(method: str, path: str, token: str, maximum: int = 256 * 1024) -> object:
    """Call SoundCloud's private web API directly with the signed-in session token."""
    if not path.startswith("/") or method not in {"GET", "PUT", "DELETE"}:
        raise ValueError("invalid API request")
    request = Request(
        "https://api-v2.soundcloud.com" + path,
        method=method,
        headers={
            "Authorization": "OAuth " + token,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with _opener("_api_opener").open(request, timeout=10.0) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("API response too large")
    return json.loads(data) if data.strip() else None

class GstPlayback:
    def __init__(self, Gst, on_change, schedule=lambda callback, *args: callback(*args)):
        self.Gst = Gst
        Gst.init(None)
        self.current = None
        self.on_change = on_change
        self.schedule = schedule
        self.active = False
        self.playing = False
        self.state = "idle"
        self.has_started = False
        self.metadata: dict[str, object] = {}
        self.error = ""
        self.generation = 0
        self.volume = 1.0
        self.ended = False
        self.bus_watches = {}
        self.hls_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=HLS_MAX_WORKERS,
            thread_name_prefix="soundcloud-hls",
        )
        self.hls_slots = threading.BoundedSemaphore(HLS_MAX_WORKERS)

    def _watch_bus(self, pipeline, generation: int) -> None:
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        handler = bus.connect("message", self._message, pipeline, generation)
        self.bus_watches[id(pipeline)] = (bus, handler)

    def _message(self, _bus, message, pipeline, generation):
        if pipeline is not self.current or generation != self.generation:
            return
        if message.type == self.Gst.MessageType.ERROR:
            self.stop("SoundCloud could not play this track")
        elif message.type == self.Gst.MessageType.EOS:
            self.playing = False
            self.state = "paused"
            self.ended = True
            self.on_change()
        elif message.type == self.Gst.MessageType.STATE_CHANGED and message.src == self.current:
            _old, current, _pending = message.parse_state_changed()
            self.playing = current == self.Gst.State.PLAYING
            if self.playing:
                self.has_started = True
                self.state = "playing"
            elif current == self.Gst.State.PAUSED:
                self.state = "paused" if self.has_started else "buffering"
            self.on_change()

    def _stop_current(self) -> None:
        self.generation += 1
        previous = self.current
        self.current = None
        if previous is None:
            return
        watch = self.bus_watches.pop(id(previous), None)
        if watch:
            bus, handler = watch
            bus.disconnect(handler)
            bus.remove_signal_watch()
        previous.set_state(self.Gst.State.NULL)

    def stop(self, error: str = "") -> None:
        self._stop_current()
        self.active = False
        self.playing = False
        self.has_started = False
        # Keep the failed track's id so the popup can show the error on its row.
        self.metadata = (
            {"playbackId": self.metadata.get("playbackId", "")} if error else {}
        )
        self.error = error[:128]
        self.state = "error" if self.error else "idle"
        self.on_change()

    def set_volume(self, volume: float) -> bool:
        self.volume = max(0.0, min(1.0, float(volume)))
        if self.current is None:
            return True
        element = self.current.get_by_name("volume") if hasattr(self.current, "get_by_name") else None
        target = element if element is not None else self.current
        try:
            target.set_property("volume", self.volume)
        except (TypeError, ValueError):
            return False
        return True

    def begin(self, metadata: dict[str, object]) -> int:
        self._stop_current()
        self.ended = False
        self.active = False
        self.playing = False
        self.has_started = False
        self.metadata = dict(metadata)
        self.error = ""
        self.state = "resolving"
        self.on_change()
        return self.generation

    def play(
        self,
        uri: str,
        metadata: dict[str, object],
        protocol: str = "",
        mime_type: str = "",
    ) -> bool:
        self.metadata = dict(metadata)
        self.error = ""
        self.active = True
        self.playing = False
        self.has_started = False
        self.ended = False
        self.state = "buffering"
        if protocol == "hls" and mime_type == "audio/mpeg":
            return self._play_hls(uri)
        pipeline = self.Gst.ElementFactory.make(
            "playbin3", f"omarchy-soundcloud-player-{self.generation}"
        )
        if pipeline is None:
            return self._fail("SoundCloud could not initialize audio playback")
        self.current = pipeline
        self._watch_bus(pipeline, self.generation)
        pipeline.set_property("uri", uri)
        pipeline.set_property("volume", self.volume)
        result = pipeline.set_state(self.Gst.State.PLAYING)
        if result == self.Gst.StateChangeReturn.FAILURE:
            self._stop_current()
            return self._fail("SoundCloud could not play this track")
        return True

    def _fail(self, message: str) -> bool:
        self.active = False
        self.error = message[:128]
        self.state = "error"
        self.on_change()
        return False

    def _play_hls(self, uri: str) -> bool:
        try:
            pipeline = self.Gst.parse_launch(
                "appsrc name=source format=time is-live=false block=true "
                "caps=audio/mpeg,mpegversion=1,layer=3 ! mpegaudioparse ! "
                "mpg123audiodec ! audioconvert ! audioresample ! "
                f"volume name=volume volume={self.volume!r} ! autoaudiosink"
            )
        except Exception:
            return self._fail("SoundCloud could not initialize audio playback")
        source = pipeline.get_by_name("source")
        if source is None:
            return self._fail("SoundCloud could not initialize audio playback")
        self.current = pipeline
        self._watch_bus(pipeline, self.generation)
        generation = self.generation
        if pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            self._stop_current()
            return self._fail("SoundCloud could not initialize audio playback")
        if not self.hls_slots.acquire(blocking=False):
            self._stop_current()
            return self._fail("SoundCloud playback is busy")
        self.hls_executor.submit(
            self._feed_hls_bounded,
            generation,
            source,
            uri,
            time.monotonic() + HLS_OPERATION_SECONDS,
        )
        return True

    def _feed_hls_bounded(self, generation: int, source, uri: str, deadline: float) -> None:
        try:
            self._feed_hls(generation, source, uri, deadline)
        finally:
            self.hls_slots.release()

    def _feed_hls(self, generation: int, source, uri: str, deadline: float) -> None:
        try:
            total_bytes = 0
            cancelled = lambda: generation != self.generation
            for segment in hls_segment_urls(uri, deadline=deadline, cancelled=cancelled):
                if generation != self.generation:
                    return
                data = fetch_private_media(
                    segment,
                    4 * 1024 * 1024,
                    deadline=deadline,
                    cancelled=cancelled,
                )
                total_bytes += len(data)
                if total_bytes > 128 * 1024 * 1024:
                    raise ValueError("track stream too large")
                buffer = self.Gst.Buffer.new_allocate(None, len(data), None)
                buffer.fill(0, data)
                if source.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
                    raise RuntimeError("audio pipeline rejected data")
            if generation == self.generation:
                source.emit("end-of-stream")
        except (OSError, RuntimeError, TimeoutError, UnicodeError, ValueError,
                concurrent.futures.CancelledError):
            if generation == self.generation:
                self.schedule(self._hls_failed, generation)

    def _hls_failed(self, generation: int) -> bool:
        if generation == self.generation:
            self.stop("SoundCloud could not stream this track")
        return False

    def toggle(self) -> bool:
        if not self.active:
            return False
        target = self.Gst.State.PAUSED if self.playing else self.Gst.State.PLAYING
        return self.current.set_state(target) != self.Gst.StateChangeReturn.FAILURE

    def pause(self) -> bool:
        if not self.active or self.current is None:
            return False
        return self.current.set_state(self.Gst.State.PAUSED) != self.Gst.StateChangeReturn.FAILURE

    def seek(self, ratio: float) -> bool:
        if not self.active:
            return False
        duration = self._query_time("query_duration")
        if duration <= 0:
            duration = float(self.metadata.get("durationMs", 0) or 0) / 1000
        if duration <= 0:
            return False
        return bool(self.current.seek_simple(
            self.Gst.Format.TIME,
            self.Gst.SeekFlags.FLUSH | self.Gst.SeekFlags.KEY_UNIT,
            int(duration * max(0.0, min(1.0, ratio)) * self.Gst.SECOND),
        ))

    def _query_time(self, method: str) -> float:
        try:
            ok, value = getattr(self.current, method)(self.Gst.Format.TIME)
            return value / self.Gst.SECOND if ok and value >= 0 else 0.0
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def status(self) -> dict[str, object]:
        if not self.active:
            return {
                "playerPresent": False,
                "playbackState": self.state,
                "playing": False,
                "playbackId": str(self.metadata.get("playbackId", "")),
                "title": str(self.metadata.get("title", "")),
                "artist": str(self.metadata.get("artist", "")),
                "artUrl": "",
                "duration": 0.0,
                "position": 0.0,
                "waveform": [],
                **({"error": self.error} if self.error else {}),
            }
        duration = self._query_time("query_duration")
        if duration <= 0:
            duration = float(self.metadata.get("durationMs", 0) or 0) / 1000
        return {
            "playerPresent": True,
            "playbackState": self.state,
            "playing": self.playing,
            "playbackId": str(self.metadata.get("playbackId", "")),
            "title": str(self.metadata.get("title", "")),
            "artist": str(self.metadata.get("artist", "")),
            "artUrl": str(self.metadata.get("artUrl", "")),
            "duration": duration,
            "position": self._query_time("query_position"),
            "waveform": list(self.metadata.get("waveform", [])),
            **({"error": self.error} if self.error else {}),
        }

    def close(self) -> None:
        self._stop_current()
        self.hls_executor.shutdown(wait=False, cancel_futures=True)

_media_opener = build_opener(ProxyHandler({}), _NoMediaRedirect)

_api_opener = build_opener(ProxyHandler({}), _NoMediaRedirect)

