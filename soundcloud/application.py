"""GTK/WebKit application factory (daemon UI + session)."""
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

from .api import *
from .artwork import *
from .commands import *
from .media import *  # noqa: F403
from .media import _load_gi
from .mpris import *  # noqa: F403
from .mpris import _NullConnection
from .paths import *
from .scripts import *
from .server import *
from .store import *
from .uri import *
from .validate import *  # noqa: F403
from .validate import _bounded_integer, _bounded_string

def build_application(show_on_start: bool = False):
    Gio, GLib, Gst, Gtk, Soup, WebKit2 = _load_gi()
    paths = profile_paths()
    prepare_profile_paths(paths)
    profile_fd = ensure_private_directory(paths.data_dir)
    cookie_store = CookieStore(profile_fd)
    cookie_records = cookie_store.load()
    if not cookie_records:
        cookie_records = cookie_store.migrate_legacy_sqlite()
    track_cache_store = PrivateJsonStore(profile_fd, "tracks.json", MAX_TRACK_CACHE_BYTES)
    track_cache = sanitize_track_cache(track_cache_store.load())
    player_state_store = PrivateJsonStore(profile_fd, "player.json", PLAYER_STATE_BYTES)
    try:
        saved_player_state = sanitize_player_state(player_state_store.load())
    except OSError:
        saved_player_state = {}

    def variant(signature, value):
        if signature == "a{sv}":
            return GLib.Variant("a{sv}", {key: variant(*entry) for key, entry in value.items()})
        return GLib.Variant(signature, value)

    class MprisService:
        """Exposes the player on D-Bus so media keys and the shell can drive it."""

        def __init__(self, app):
            self.app = app
            self.connection = None
            self.registrations = []
            self.properties: dict[str, tuple[str, object]] = {}
            self.position = 0.0
            self.duration = 0.0
            self.playing = False
            self.node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
            self.owner_id = Gio.bus_own_name(
                Gio.BusType.SESSION, MPRIS_BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                self._acquired, None, None,
            )

        def _acquired(self, connection, _name):
            self.connection = connection
            for interface in self.node.interfaces:
                self.registrations.append(connection.register_object(
                    MPRIS_PATH, interface, self._method, self._get, self._set,
                ))

        def _get(self, _connection, _sender, _path, interface, name):
            if interface == MPRIS_ROOT and name in MPRIS_ROOT_PROPERTIES:
                return variant(*MPRIS_ROOT_PROPERTIES[name])
            if name == "Position":
                return GLib.Variant("x", int(self.position * 1_000_000))
            entry = self.properties.get(name)
            return variant(*entry) if entry else None

        def _set(self, _connection, _sender, _path, interface, name, value):
            if interface == MPRIS_PLAYER and name == "Volume":
                self.app._run_internal(f"volume:{max(0.0, min(1.0, value.unpack())):.3f}")
            return True

        def _seek_to(self, seconds):
            if self.duration > 0:
                self.app._run_internal(f"seek:{max(0.0, min(1.0, seconds / self.duration)):.5f}")

        def _method(self, _connection, _sender, _path, interface, method, parameters, invocation):
            commands = {
                "Raise": "show", "PlayPause": "play-pause", "Next": "next", "Previous": "previous",
            }
            if method in commands:
                self.app._run_internal(commands[method])
            elif method == "Play" and not self.playing:
                self.app._run_internal("play-pause")
            elif method in {"Pause", "Stop"} and self.playing:
                self.app._run_internal("play-pause")
            elif method == "Seek":
                self._seek_to(self.position + parameters.unpack()[0] / 1_000_000)
            elif method == "SetPosition":
                self._seek_to(parameters.unpack()[1] / 1_000_000)
            invocation.return_value(None)

        def update(self, state, art_url, volume):
            self.position = float(state.get("position") or 0)
            self.duration = float(state.get("duration") or 0)
            self.playing = state.get("playing") is True
            properties = mpris_player_properties(state, art_url, volume)
            changed = {
                name: entry for name, entry in properties.items()
                if self.properties.get(name) != entry
            }
            self.properties = properties
            if changed and self.connection is not None:
                self.connection.emit_signal(
                    None, MPRIS_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                    GLib.Variant("(sa{sv}as)", (
                        MPRIS_PLAYER,
                        {name: variant(*entry) for name, entry in changed.items()},
                        [],
                    )),
                )

        def close(self):
            if self.connection is not None:
                for registration in self.registrations:
                    self.connection.unregister_object(registration)
            Gio.bus_unown_name(self.owner_id)
    socket_path = runtime_socket_path()
    prepare_runtime_socket_path(socket_path)
    install_path = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".config/omarchy/plugins/brunosilveira.soundcloud"

    GLib.set_application_name(APP_NAME)
    GLib.set_prgname("omarchy-soundcloud")

    class SoundCloudApplication(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.window = None
            self.webview = None
            self.status_subscribers = StatusSubscribers()
            self.status_evaluation_pending = False
            self.artwork_cache = ArtworkCache()
            self.playback = GstPlayback(
                Gst,
                lambda: GLib.idle_add(self._playback_changed),
                GLib.idle_add,
            )
            self.list_artwork_cache = ArtworkCache(
                maximum_entries=48,
                maximum_pending=8,
                fetcher=lambda uri: fetch_artwork_data_url(
                    uri,
                    max_bytes=LIST_ARTWORK_BYTES,
                    max_pixels=LIST_ARTWORK_PIXELS,
                    max_dimension=LIST_ARTWORK_DIMENSION,
                ),
            )
            self.track_artwork_urls: OrderedDict[str, str] = OrderedDict()
            self.track_likes = TrackLikes(
                lambda: oauth_token_from_cookies(cookie_store.load()),
                self._write_like_in_page,
            )
            self.track_cache = track_cache
            self.playback_tracks = {
                track["playbackId"]: track
                for tracks in track_cache.values()
                for track in tracks
                if is_playback_id(track.get("playbackId"))
            }
            self.selection_generation = 0
            # True while SoundCloud's own web player (inside the WebKit view) is audible.
            self.page_playing = False
            # True while the web player has a track loaded, playing or paused.
            self.page_media_present = False
            # Likes, search, and opened playlist/artist lists (Home/Feed live in track_cache).
            self.lists: dict[str, list[dict[str, object]]] = {}
            # The list the current track was played from; next/previous walk it.
            self.queue: list[dict[str, object]] = []
            self.queue_index = -1
            self.queue_source = ""
            # Consecutive unavailable-track skips; resets when a track actually plays.
            self.consecutive_unavailable_skips = 0
            # Home/Feed answered from cache instead of reloading the page.
            self.cached_sources: set[str] = set()
            self.pending_seek = 0.0
            self.last_art_url = ""
            self.last_state_save = 0.0
            self.mpris = None
            self.restored: dict[str, object] | None = None
            self.playback.volume = float(saved_player_state.get("volume", 1.0))
            if saved_player_state.get("track"):
                track = saved_player_state["track"]
                self.restored = {"track": track, "position": saved_player_state.get("position", 0.0)}
                self.playback_tracks[track["playbackId"]] = track
                if saved_player_state.get("queue"):
                    self.queue = list(saved_player_state["queue"])
                    self.queue_index = saved_player_state["queueIndex"]
                    self.queue_source = saved_player_state["queueSource"]
                    for queued in self.queue:
                        self.playback_tracks.setdefault(queued["playbackId"], queued)
            self.track_cache_save_pending = False
            self.had_subscriber = False
            self.disconnect_generation = 0
            self.cookie_save_source = 0
            self.cookie_manager = None
            self.dispatch_slots = threading.BoundedSemaphore(MAX_PENDING_DISPATCHES)
            self.control_server = ControlServer(
                socket_path,
                self._dispatch_from_thread,
                self._disconnect_from_thread,
            )

        def do_activate(self):
            self._create_window()
            self.hold()
            try:
                self.mpris = MprisService(self)
            except Exception:
                self.mpris = None
            self.control_server.start()
            GLib.timeout_add(750, self._publish_status)
            GLib.timeout_add_seconds(5, self._check_installation)
            if show_on_start:
                self.show_login()
            else:
                self.window.show_all()
                GLib.idle_add(self.window.hide)

        def do_shutdown(self):
            self._save_player_state()
            if self.mpris is not None:
                self.mpris.close()
            self.control_server.close()
            self.playback.close()
            self.artwork_cache.close()
            self.list_artwork_cache.close()
            os.close(profile_fd)
            Gtk.Application.do_shutdown(self)

        def _check_installation(self):
            if not install_path.exists():
                self.quit()
                return False
            return True

        def _create_window(self):
            manager = WebKit2.WebsiteDataManager.new_ephemeral()
            context = WebKit2.WebContext.new_with_website_data_manager(manager)
            cookies = context.get_cookie_manager()
            self.cookie_manager = cookies
            cookies.set_accept_policy(WebKit2.CookieAcceptPolicy.ALWAYS)
            cookies.connect("changed", self._cookies_changed)

            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title("SoundCloud sign in — close when finished")
            self.window.set_default_size(1080, 760)
            self.window.set_icon_name("audio-x-generic")
            self.window.connect("delete-event", self._hide_window)

            self.webview = WebKit2.WebView.new_with_context(context)
            content_manager = self.webview.get_user_content_manager()
            if not content_manager.register_script_message_handler("omarchyTracks"):
                raise RuntimeError("could not register SoundCloud track message handler")
            if not content_manager.register_script_message_handler("omarchyPlayback"):
                raise RuntimeError("could not register SoundCloud playback message handler")
            content_manager.connect(
                "script-message-received::omarchyTracks",
                self._tracks_message_received,
            )
            content_manager.connect(
                "script-message-received::omarchyPlayback",
                self._playback_message_received,
            )
            content_manager.add_script(WebKit2.UserScript.new(
                api_request_capture_script(),
                WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.START,
                ["https://soundcloud.com/*"],
                [],
            ))
            settings = self.webview.get_settings()
            for name, value in (
                ("enable-webaudio", True),
                ("enable-mediasource", True),
                ("enable-media-stream", True),
                ("enable-media", True),
                ("media-playback-requires-user-gesture", False),
            ):
                settings.set_property(name, value)
            self.webview.connect("load-changed", self._page_load_changed)
            self.webview.connect("create", self._create_popup)
            self.webview.connect("decide-policy", self._decide_policy)
            self.window.add(self.webview)
            restored = []
            now = int(time.time())
            for record in cookie_records:
                expires = record["expires"]
                max_age = -1 if expires is None else max(0, int(expires) - now)
                cookie = Soup.Cookie.new(
                    record["name"],
                    record["value"],
                    record["domain"],
                    record["path"],
                    max_age,
                )
                cookie.set_secure(record["secure"])
                cookie.set_http_only(record["httpOnly"])
                restored.append(cookie)
            if restored:
                cookies.replace_cookies(restored, None, self._cookies_restored, None)
            else:
                self.webview.load_uri(START_URI)

        def _tracks_message_received(self, _manager, message):
            try:
                value = message.get_js_value()
                encoded = value.to_json(0)
                raw = json.loads(encoded)
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    return
                payload = json.loads(raw)
                source = payload.get("source") if isinstance(payload, dict) else None
                if source not in {"home", "feed"}:
                    return
                page = validate_tracks_payload({"tracks": payload.get("tracks")})
                if page is None:
                    return
                incoming_tracks = page.get("tracks")
                if not isinstance(incoming_tracks, list):
                    return
                for track in incoming_tracks:
                    playback_id = track.get("playbackId")
                    if is_playback_id(playback_id):
                        self.playback_tracks[playback_id] = track
                while len(self.playback_tracks) > MAX_TRACKS * 2:
                    self.playback_tracks.pop(next(iter(self.playback_tracks)))
                existing_tracks = self.track_cache.get(source, [])
                reset = payload.get("reset") is True
                merged_tracks = merge_track_pages(
                    existing_tracks,
                    incoming_tracks,
                    reset=reset,
                )
                previous_count = 0 if reset else len(existing_tracks)
                added_count = max(0, len(merged_tracks) - previous_count)
                pagination_error = _bounded_string(payload.get("error", ""), 128) or ""
                has_more = payload.get("hasMore") is True and (
                    added_count > 0 or bool(pagination_error)
                )
                result = self._secure_tracks({"tracks": merged_tracks})
                if result is None:
                    return
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return
            self.status_subscribers.publish_message(
                {
                    "type": "tracks",
                    "source": source,
                    "reset": reset,
                    "hasMore": has_more,
                    "addedCount": added_count,
                    **({"error": pagination_error} if pagination_error else {}),
                    **result,
                }
            )
            log_event(
                "track-page",
                source=source,
                reset=reset,
                incomingCount=len(incoming_tracks),
                previousCount=previous_count,
                resultCount=len(merged_tracks),
                addedCount=added_count,
                hasMore=has_more,
            )
            if result["tracks"]:
                self.track_cache[source] = merged_tracks
                if not self.track_cache_save_pending:
                    self.track_cache_save_pending = True
                    GLib.idle_add(self._save_track_cache)

        def _playback_message_received(self, _manager, message):
            try:
                value = message.get_js_value()
                encoded = value.to_json(0)
                raw = json.loads(encoded)
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32768:
                    return
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    return
                if payload.get("event") == "page-play":
                    # The web player started: it wins, so only one player is audible.
                    self.page_playing = True
                    if self.playback.playing:
                        self.playback.pause()
                    GLib.timeout_add(150, self._publish_status_once)
                    return
                request_id = payload.get("requestId")
                if (isinstance(request_id, bool) or not isinstance(request_id, int)
                        or request_id != self.selection_generation):
                    return
                if payload.get("ok") is not True:
                    self.playback.stop(
                        _bounded_string(payload.get("error", ""), 128)
                        or "SoundCloud could not resolve this track"
                    )
                    return
                stream_uri = validate_stream_uri(payload.get("streamUrl"))
                validated = validate_tracks_payload({"tracks": [payload.get("track")]})
                validated_tracks = validated.get("tracks") if validated is not None else None
                if (stream_uri is None or not isinstance(validated_tracks, list)
                        or len(validated_tracks) != 1):
                    self.playback.stop("SoundCloud returned invalid stream details")
                    return
                selected_track = validated_tracks[0]
                if not isinstance(selected_track, dict):
                    self.playback.stop("SoundCloud returned invalid track details")
                    return
                if selected_track.get("playbackId") != self.playback.metadata.get("playbackId"):
                    return
                protocol = payload.get("protocol") if payload.get("protocol") in {"hls", "progressive"} else ""
                mime_type = payload.get("mimeType") if payload.get("mimeType") in {"audio/mpeg", "audio/mp4"} else ""
                started = self.playback.play(
                    stream_uri,
                    {**selected_track, "waveform": validate_waveform(payload.get("waveform"))},
                    protocol,
                    mime_type,
                )
                if not started:
                    self.playback.error = "SoundCloud could not play this track"
                self._publish_status_once()
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return

        def _save_track_cache(self):
            self.track_cache_save_pending = False
            try:
                track_cache_store.save(self.track_cache)
            except OSError:
                pass
            return False

        def _publish_cached_tracks(self, source):
            cached = self.track_cache.get(source)
            if not cached:
                return
            result = self._secure_tracks({"tracks": cached})
            if result is not None and result["tracks"]:
                self.status_subscribers.publish_message(
                    {"type": "tracks", "source": source, "cached": True, **result}
                )

        def _cookies_restored(self, manager, result, _data):
            try:
                manager.replace_cookies_finish(result)
            except Exception:
                pass
            self.webview.load_uri(START_URI)

        def _cookies_changed(self, _manager):
            if self.cookie_save_source:
                GLib.source_remove(self.cookie_save_source)
            self.cookie_save_source = GLib.timeout_add_seconds(2, self._save_cookies)

        def _save_cookies(self):
            self.cookie_save_source = 0
            self.cookie_manager.get_all_cookies(None, self._cookies_collected, None)
            return False

        def _cookies_collected(self, manager, result, _data):
            try:
                cookies = manager.get_all_cookies_finish(result)
                records = []
                for cookie in cookies[:MAX_COOKIES]:
                    domain = cookie.get_domain() or ""
                    normalized = domain.lower().lstrip(".").rstrip(".")
                    if normalized != "soundcloud.com" and not normalized.endswith(".soundcloud.com"):
                        continue
                    expires = cookie.get_expires()
                    records.append({
                        "name": cookie.get_name() or "",
                        "value": cookie.get_value() or "",
                        "domain": domain,
                        "path": cookie.get_path() or "/",
                        "expires": int(expires.to_unix()) if expires else None,
                        "secure": bool(cookie.get_secure()),
                        "httpOnly": bool(cookie.get_http_only()),
                    })
                cookie_store.save(records)
            except Exception:
                pass

        def _page_load_changed(self, _webview, event):
            if event == WebKit2.LoadEvent.FINISHED:
                # A fresh page forgets the volume; hand it over again.
                self._evaluate(volume_script(self.playback.volume), lambda _payload: None)

        def _run_internal(self, command):
            if not self.dispatch_slots.acquire(blocking=False):
                return
            GLib.idle_add(self._handle_command_safely, command, _NullConnection(), None)

        def _list_tracks(self, source):
            if source in {"home", "feed"}:
                return self.track_cache.get(source, [])
            return self.lists.get(source, [])

        def _set_queue(self, source, playback_id):
            tracks = [
                track for track in self._list_tracks(source)
                if is_playback_id(track.get("playbackId"))
            ][:MAX_QUEUE]
            ids = [track["playbackId"] for track in tracks]
            if playback_id not in ids:
                return False
            self.queue = list(tracks)
            self.queue_index = ids.index(playback_id)
            self.queue_source = source
            for track in tracks:
                self.playback_tracks.setdefault(track["playbackId"], track)
            return True

        def _queue_for_unsourced_play(self, playback_id):
            for source in ("search", "collection", "likes", "home", "feed"):
                if self._set_queue(source, playback_id):
                    return
            self.queue, self.queue_index, self.queue_source = [], -1, ""

        def _start_track(self, playback_id, done=None, resume_at=0.0):
            finish = done or (lambda _ok, _error: None)
            metadata = self.playback_tracks.get(playback_id)
            if metadata is None:
                finish(False, "SoundCloud track details are not ready")
                return
            self.restored = None
            self.pending_seek = max(0.0, float(resume_at))
            self.selection_generation = (self.selection_generation + 1) % (2**31)
            generation = self.selection_generation
            self.playback.begin(metadata)
            self._publish_status_once()

            def track_selected(payload):
                if generation != self.selection_generation:
                    finish(False, "selection superseded")
                    return
                if not payload:
                    self.playback.stop("SoundCloud track details are not ready")
                finish(bool(payload), "" if payload else "SoundCloud track details are not ready")

            self._evaluate(play_track_script(playback_id, generation), track_selected)
            GLib.timeout_add_seconds(15, self._resolution_timed_out, generation)

        def _advance(self, step, done=None):
            target = queue_step(len(self.queue), self.queue_index, step)
            if target is None and step == 1 and self.queue:
                # The list is over; carry on with tracks related to the last one.
                self._autoplay_related(done)
                return True
            if target is None:
                if done:
                    done(False, "No more tracks in this list")
                return False
            self.queue_index = target
            self._start_track(self.queue[target]["playbackId"], done)
            return True

        def _autoplay_related(self, done=None):
            finish = done or (lambda _ok, _error: None)
            last = self.queue[self.queue_index] if 0 <= self.queue_index < len(self.queue) else self.queue[-1]
            playback_id = str(last.get("playbackId") or "")
            if not is_playback_id(playback_id) or not self.webview \
                    or not is_soundcloud_uri(self.webview.get_uri() or ""):
                finish(False, "No more tracks in this list")
                return
            generation = self.selection_generation

            def finished(webview, async_result, _data):
                try:
                    value = webview.call_async_javascript_function_finish(async_result)
                    raw = value.to_string() if value.is_string() else ""
                    payload = json.loads(raw) if raw and len(raw.encode("utf-8")) <= MAX_RESPONSE_BYTES else {}
                except Exception:
                    payload = {}
                validated = validate_tracks_payload({"tracks": payload.get("tracks", [])}) \
                    if isinstance(payload, dict) else None
                if generation != self.selection_generation:
                    finish(False, "selection superseded")
                    return
                queue, index = extend_queue(self.queue, self.queue_index, validated["tracks"] if validated else [])
                if index + 1 >= len(queue):
                    finish(False, "No more tracks in this list")
                    return
                for track in queue[index + 1:]:
                    self.playback_tracks.setdefault(track["playbackId"], track)
                self.queue, self.queue_index = queue, index + 1
                self._start_track(queue[index + 1]["playbackId"], finish)

            self.webview.call_async_javascript_function(
                RELATED_TRACKS_FUNCTION, -1,
                GLib.Variant("a{sv}", {"id": GLib.Variant("d", float(playback_id.rpartition(":")[2]))}),
                None, None, None, finished, None,
            )

        def _playback_changed(self):
            if self.pending_seek and self.playback.playing:
                duration = float(self.playback.metadata.get("durationMs", 0) or 0) / 1000
                if duration > 0:
                    self.playback.seek(min(1.0, self.pending_seek / duration))
                self.pending_seek = 0.0
            if self.playback.ended:
                self.playback.ended = False
                self.consecutive_unavailable_skips = 0
                self._advance(1)
            elif should_auto_skip_unavailable(
                self.playback.state,
                len(self.queue),
                self.consecutive_unavailable_skips,
                self.page_playing,
            ):
                # Resolve failures, GStreamer errors, and dead streams all land here.
                self.consecutive_unavailable_skips += 1
                self._advance(1)
            if self.playback.state in {"playing", "paused"}:
                self.consecutive_unavailable_skips = 0
                self._save_player_state()
            self._publish_status_once()
            return False

        def _player_snapshot(self):
            snapshot: dict[str, object] = {"volume": self.playback.volume}
            if self.playback.active:
                track = self.playback.metadata
                position = self.playback._query_time("query_position")
            elif self.restored:
                track = self.restored["track"]
                position = self.restored.get("position", 0.0)
            else:
                return snapshot
            if not is_playback_id(track.get("playbackId")):
                return snapshot
            snapshot["track"] = {
                key: track.get(key) for key in
                ("playbackId", "title", "artist", "url", "artUrl", "playCount", "durationMs")
            }
            snapshot["position"] = max(0.0, float(position or 0))
            if self.queue and 0 <= self.queue_index < len(self.queue):
                snapshot["queue"] = self.queue
                snapshot["queueIndex"] = self.queue_index
                snapshot["queueSource"] = self.queue_source
            return snapshot

        def _save_player_state(self):
            self.last_state_save = time.monotonic()
            try:
                player_state_store.save(self._player_snapshot())
            except (OSError, TypeError, ValueError):
                pass

        def _hide_window(self, *_args):
            self.window.hide()
            # Closing the window stops SoundCloud's web player; the bar's own
            # player is unaffected.
            self._evaluate(command_script("pause-page"), lambda _payload: self._publish_status_once())
            return True

        def show_login(self):
            self.window.show_all()
            self.window.present()

        def _create_popup(self, source, _navigation_action):
            popup = WebKit2.WebView.new_with_related_view(source)
            popup.connect("decide-policy", self._decide_popup_policy)
            popup_window = Gtk.Window(application=self)
            popup_window.set_default_size(720, 720)
            popup_window.set_title("SoundCloud sign in")
            popup_window.add(popup)
            popup.connect("close", lambda *_: popup_window.destroy())
            popup_window.show_all()
            return popup

        def _decide_policy(self, _webview, decision, decision_type):
            if decision_type not in {
                WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
            }:
                return False
            request = decision.get_request()
            uri = request.get_uri() if request else ""
            if is_soundcloud_uri(uri) or is_bot_check_uri(uri):
                decision.use()
            else:
                decision.ignore()
            return True

        def _decide_popup_policy(self, _webview, decision, decision_type):
            if decision_type not in {
                WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
            }:
                return False
            request = decision.get_request()
            uri = request.get_uri() if request else ""
            if is_soundcloud_uri(uri) or is_bot_check_uri(uri) or is_sign_in_provider_uri(uri):
                decision.use()
            else:
                decision.ignore()
            return True

        def _dispatch_from_thread(self, command, connection, request_id):
            if not self.dispatch_slots.acquire(blocking=False):
                send_response(connection, {"ok": False, "error": "server busy"}, request_id)
                return
            GLib.idle_add(self._handle_command_safely, command, connection, request_id)

        def _handle_command_safely(self, command, connection, request_id):
            try:
                return self._handle_command(command, connection, request_id)
            except Exception:
                self._respond(connection, request_id, {"ok": False, "error": "request failed"})
                return False

        def _disconnect_from_thread(self, connection):
            self.status_subscribers.remove(connection)
            if self.had_subscriber and self.status_subscribers.count() == 0:
                self.disconnect_generation += 1
                generation = self.disconnect_generation
                GLib.timeout_add_seconds(10, self._quit_if_abandoned, generation)

        def _quit_if_abandoned(self, generation):
            if generation == self.disconnect_generation and self.status_subscribers.count() == 0:
                self.quit()
            return False

        def _artwork_ready_from_thread(self):
            GLib.idle_add(self._publish_status_once)

        def _secure_status(self, payload):
            if isinstance(payload, dict) and "pagePlaying" in payload:
                self.page_playing = payload.get("pagePlaying") is True
                self.page_media_present = payload.get("playerPresent") is True
            if (self.restored and self.playback.state == "idle"
                    and not self.page_playing and not self.page_media_present):
                # The last track from before a restart, ready to resume.
                track = self.restored["track"]
                base = payload if isinstance(payload, dict) else {}
                payload = {
                    **base,
                    "playerPresent": True,
                    "playbackState": "paused",
                    "playing": False,
                    "playbackId": track["playbackId"],
                    "title": track["title"],
                    "artist": track["artist"],
                    "artUrl": track.get("artUrl", ""),
                    "duration": float(track.get("durationMs", 0) or 0) / 1000,
                    "position": float(self.restored.get("position", 0.0)),
                    "waveform": [],
                }
            # Show whichever player is audible; the bar's player only when the
            # web player is not.
            if self.playback.state != "idle" and not (self.page_playing and not self.playback.playing):
                base = payload if isinstance(payload, dict) else {}
                payload = {**base, **self.playback.status()}
            elif self.page_playing and isinstance(payload, dict):
                payload = {**payload, "playing": True, "playbackState": "playing"}
            state = validate_status_payload(payload)
            if state is None:
                return {}
            # A fixed mid-size rendition: SoundCloud's full-size artwork can
            # exceed the fetcher's limits, and the bar only needs 300px.
            remote_art = normalize_list_artwork_url(state.pop("artUrl"), "t300x300")
            self.last_art_url = remote_art
            state["artDataUrl"] = self.artwork_cache.get(
                remote_art, self._artwork_ready_from_thread
            )
            playback_id = str(state["playbackId"])
            liked = None
            if playback_id:
                liked = self.track_likes.is_liked(int(playback_id.rpartition(":")[2]))
                if liked is None and state["loggedIn"]:
                    self.track_likes.load_in_background(
                        lambda: GLib.idle_add(self._publish_status_once)
                    )
            state["liked"] = liked is True
            state["volume"] = round(self.playback.volume, 3)
            state["queueSource"] = self.queue_source if self.queue else ""
            return state

        def _secure_tracks(self, payload):
            result = validate_tracks_payload(payload)
            if result is None:
                return None
            for track in result["tracks"]:
                original_art = track.pop("artUrl")
                remote_art = normalize_list_artwork_url(original_art)
                track["artworkId"] = ""
                if not remote_art:
                    continue
                token = hashlib.sha256(remote_art.encode("utf-8")).hexdigest()[:24]
                self.track_artwork_urls[token] = remote_art
                self.track_artwork_urls.move_to_end(token)
                while len(self.track_artwork_urls) > 200:
                    self.track_artwork_urls.popitem(last=False)
                track["artworkId"] = token
            return result

        def _respond_track_artwork(self, connection, request_id, artwork_id, data_url):
            self._respond(
                connection,
                request_id,
                {"ok": bool(data_url), "artworkId": artwork_id, "artDataUrl": data_url},
            )
            return False

        def _publish_status_once(self):
            self._publish_status()
            return False

        def _publish_status(self):
            # Runs even without a bar subscriber so MPRIS stays current.
            if self.status_evaluation_pending:
                return True
            self.status_evaluation_pending = True

            def finished(payload):
                self.status_evaluation_pending = False
                state = self._secure_status(payload)
                if self.mpris is not None and state:
                    self.mpris.update(state, self.last_art_url, self.playback.volume)
                if self.playback.playing and time.monotonic() - self.last_state_save > 15:
                    self._save_player_state()
                if self.status_subscribers:
                    self.status_subscribers.publish({"running": True, **state})

            self._evaluate(status_script(), finished)
            return True

        def _resolution_timed_out(self, generation):
            if (generation == self.selection_generation
                    and self.playback.state == "resolving"):
                self.selection_generation += 1
                self.playback.stop("SoundCloud took too long to resolve this track")
            return False

        def _respond(self, connection, request_id, payload, publish_status=False):
            try:
                send_response(connection, payload, request_id=request_id)
                if publish_status:
                    GLib.timeout_add(80, self._publish_status_once)
            finally:
                self.dispatch_slots.release()

        def _handle_command(self, command, connection, request_id):
            command = normalize_backend_command(command)
            if command == "status":
                self._evaluate(
                    status_script(),
                    lambda payload: self._respond(
                        connection,
                        request_id,
                        {"ok": True, "running": True, **self._secure_status(payload)},
                    ),
                )
            elif command == "subscribe":
                self.status_subscribers.add(connection)
                self.had_subscriber = True
                self.disconnect_generation += 1
                self._respond(connection, request_id, {"ok": True, "running": True})
                GLib.idle_add(self._publish_status_once)
            elif (source := load_more_source_from_command(command)) is not None:
                def load_more_finished(payload):
                    valid = isinstance(payload, dict)
                    started = valid and payload.get("started") is True
                    has_more = valid and payload.get("hasMore") is True
                    log_event(
                        "lazy-load-request",
                        source=source,
                        started=started,
                        hasMore=has_more,
                    )
                    self._respond(
                        connection,
                        request_id,
                        {
                            "ok": valid,
                            "running": True,
                            "started": started,
                            "hasMore": has_more,
                        },
                    )

                self._evaluate(
                    load_more_script(source),
                    load_more_finished,
                )
            elif (source := track_source_from_command(command)) in self.cached_sources:
                result = self._secure_tracks({"tracks": self.track_cache.get(source, [])})
                self._respond(connection, request_id, {
                    "ok": result is not None, "running": True, "cached": True,
                    **(result or {"tracks": []}),
                })
            elif source is not None:
                def tracks_finished(payload):
                    pending = isinstance(payload, dict) and payload.get("pending") is True
                    remote_error = (
                        _bounded_string(payload.get("error", ""), 128)
                        if isinstance(payload, dict)
                        else None
                    ) or ""
                    result = self._secure_tracks(payload)
                    if pending and result is not None:
                        result["pending"] = True
                    if remote_error and result is not None:
                        result["error"] = remote_error
                    self._respond(
                        connection,
                        request_id,
                        {
                            "ok": result is not None,
                            "running": True,
                            **(result or {"tracks": [], "error": "invalid track data"}),
                        },
                    )

                self._evaluate(
                    api_tracks_script(source),
                    tracks_finished,
                )
            elif command == "inspect-api":
                def observation_finished(payload):
                    paths = payload.get("paths", []) if isinstance(payload, dict) else []
                    paths = [
                        path for path in paths[:32]
                        if isinstance(path, str) and len(path.encode("utf-8")) <= 512
                    ]
                    lists = payload.get("lists", {}) if isinstance(payload, dict) else {}
                    safe_lists = {
                        source: {
                            "count": _bounded_integer(state.get("count"), 0, MAX_TRACKS),
                            "loading": state.get("loading") is True,
                            "error": _bounded_string(state.get("error", ""), 128) or "",
                        }
                        for source, state in lists.items()
                        if source in {"home", "feed"} and isinstance(state, dict)
                    }
                    playback_count = _bounded_integer(
                        payload.get("playbackCount"), 0, MAX_TRACKS * 2
                    ) if isinstance(payload, dict) else 0
                    self._respond(connection, request_id, {
                        "ok": True,
                        "paths": paths,
                        "lists": safe_lists,
                        "playbackCount": playback_count,
                    })

                self._evaluate(api_observation_script(), observation_finished)
            elif command.startswith("artwork:"):
                artwork_id = command.partition(":")[2]
                remote_art = self.track_artwork_urls.get(artwork_id)
                if (
                    len(artwork_id) != 24
                    or any(character not in "0123456789abcdef" for character in artwork_id)
                    or not remote_art
                ):
                    self._respond(connection, request_id, {"ok": False, "error": "unknown artwork"})
                else:
                    self.list_artwork_cache.request(
                        remote_art,
                        lambda data_url: GLib.idle_add(
                            self._respond_track_artwork,
                            connection,
                            request_id,
                            artwork_id,
                            data_url,
                        ),
                    )
            elif command == "play-pause":
                # Act on whichever player is audible, and never leave two playing.
                def toggle_audible_player(payload):
                    page_playing = isinstance(payload, dict) and payload.get("pagePlaying") is True
                    self.page_playing = page_playing
                    respond = lambda result: self._respond(
                        connection,
                        request_id,
                        {"ok": bool(result), "running": True},
                        publish_status=True,
                    )
                    if page_playing:
                        if self.playback.playing:
                            self.playback.pause()
                        self._evaluate(command_script("pause-page"), respond)
                    elif self.playback.active:
                        respond(self.playback.toggle())
                    elif (self.restored and self.playback.state in {"idle", "error"}
                            and not (isinstance(payload, dict) and payload.get("playerPresent") is True)):
                        # Resume the track from before the restart where it stopped.
                        restored = self.restored
                        self._start_track(
                            restored["track"]["playbackId"],
                            lambda ok, error: self._respond(
                                connection, request_id,
                                {"ok": ok, "running": True, **({"error": error} if error else {})},
                                publish_status=True,
                            ),
                            resume_at=float(restored.get("position", 0.0)),
                        )
                    else:
                        self._evaluate(command_script("play-pause"), respond)

                self._evaluate(status_script(), toggle_audible_player)
            elif command in {"next", "previous"}:
                respond_step = lambda ok, error="": self._respond(
                    connection, request_id,
                    {"ok": ok, "running": True, **({"error": error} if error else {})},
                    publish_status=True,
                )
                bar_player_current = (
                    self.playback.active or self.playback.state in {"resolving", "error"}
                    or bool(self.restored)
                )
                if self.queue and bar_player_current and not self.page_playing:
                    # Walk the list the current track came from.
                    position = self.playback._query_time("query_position") if self.playback.active else 0.0
                    if command == "previous" and position > 3:
                        respond_step(self.playback.seek(0.0))
                    else:
                        self._advance(1 if command == "next" else -1, respond_step)
                elif command == "previous" and self.playback.active and not self.page_playing:
                    respond_step(self.playback.seek(0.0))
                else:
                    if self.playback.active or self.playback.error:
                        self.playback.stop()
                    self.restored = None
                    self._evaluate(
                        command_script(command),
                        lambda payload: respond_step(bool(payload)),
                    )
            elif (search := search_from_command(command)) is not None:
                kind, query = search
                self._page_list(
                    SEARCH_TRACKS_FUNCTION,
                    {"query": GLib.Variant("s", query), "kind": GLib.Variant("s", kind)},
                    "search" if kind == "tracks" else "",
                    connection, request_id, {"query": query, "kind": kind},
                )
            elif command.partition(":")[0] in SEARCH_KINDS:
                self._respond(connection, request_id, {"ok": False, "error": "Enter a shorter search"})
            elif (collection := collection_from_command(command)) is not None:
                kind, identifier = collection
                self._page_list(
                    OPEN_COLLECTION_FUNCTION,
                    {"kind": GLib.Variant("s", kind), "id": GLib.Variant("d", float(identifier))},
                    "collection", connection, request_id, {"kind": kind, "collectionId": identifier},
                )
            elif command == "likes":
                self._page_list(LIKED_TRACKS_FUNCTION, {}, "likes", connection, request_id, {})
            elif command == "mixes":
                self._load_mixes(connection, request_id, 0)
            elif (shuffle_source := shuffle_from_command(command)) is not None:
                current = self.queue[self.queue_index].get("playbackId", "") if 0 <= self.queue_index < len(self.queue) else ""
                tracks = shuffled_tracks(self._list_tracks(shuffle_source), current)
                if not tracks:
                    self._respond(connection, request_id, {"ok": False, "error": "Nothing to shuffle yet"})
                else:
                    self.queue, self.queue_index, self.queue_source = tracks, 0, shuffle_source
                    for track in tracks:
                        self.playback_tracks.setdefault(track["playbackId"], track)

                    def shuffle_started(ok, error):
                        self._respond(
                            connection, request_id,
                            {"ok": ok, "running": True, "shuffled": len(tracks), **({"error": error} if error else {})},
                            publish_status=ok or error != "selection superseded",
                        )

                    self._start_track(tracks[0]["playbackId"], shuffle_started)
            elif (volume := volume_from_command(command)) is not None:
                self.playback.set_volume(volume)
                self._evaluate(volume_script(volume), lambda _payload: None)
                self._save_player_state()
                self._respond(
                    connection, request_id,
                    {"ok": True, "running": True, "volume": volume},
                    publish_status=True,
                )
            elif command.startswith("seek:"):
                try:
                    ratio = float(command.partition(":")[2])
                except ValueError:
                    self._respond(
                        connection,
                        request_id,
                        {"ok": False, "error": "invalid seek position"},
                    )
                else:
                    if self.playback.active and not self.page_playing:
                        self._respond(
                            connection,
                            request_id,
                            {"ok": self.playback.seek(ratio), "running": True},
                            publish_status=True,
                        )
                    else:
                        self._evaluate(
                            seek_script(ratio),
                            lambda payload: self._respond(
                                connection,
                                request_id,
                                {"ok": bool(payload), "running": True},
                                publish_status=True,
                            ),
                        )
            elif ((queued := queued_play_from_command(command)) is not None
                    or playback_id_from_command(command) is not None):
                if queued is not None:
                    source, playback_id = queued
                    if not self._set_queue(source, playback_id):
                        self._queue_for_unsourced_play(playback_id)
                else:
                    playback_id = playback_id_from_command(command)
                    self._queue_for_unsourced_play(playback_id)

                def track_started(ok, error):
                    self._respond(
                        connection,
                        request_id,
                        {"ok": ok, "running": True, **({"error": error} if error else {})},
                        publish_status=ok or error != "selection superseded",
                    )

                self._start_track(playback_id, track_started)
            elif (like := track_like_from_command(command)) is not None:
                playback_id, liked = like

                def update_like():
                    try:
                        self.track_likes.set_liked(int(playback_id.rpartition(":")[2]), liked)
                        payload = {"ok": True, "running": True, "playbackId": playback_id, "liked": liked}
                    except BotCheckRequired:
                        GLib.idle_add(self.show_login)
                        payload = {"ok": False, "error": BOT_CHECK_MESSAGE}
                    except (OSError, ValueError):
                        payload = {"ok": False, "error": "SoundCloud could not update this like"}
                    GLib.idle_add(
                        lambda: self._respond(connection, request_id, payload, publish_status=True)
                    )

                threading.Thread(target=update_like, daemon=True).start()
            elif source_uri(command) or (
                    command.startswith("refresh:") and source_uri(command.removeprefix("refresh:"))):
                forced = command.startswith("refresh:")
                source = command.removeprefix("refresh:")
                if (not forced and source in {"home", "feed"} and self.track_cache.get(source)
                        and (self.page_media_present or self.page_playing)):
                    # Reloading would drop the web player's track; answer from cache.
                    self.cached_sources.add(source)
                    self._publish_cached_tracks(source)
                    self._respond(connection, request_id, {"ok": True, "running": True, "cached": True})
                else:
                    self.cached_sources.discard(source)
                    self._publish_cached_tracks(source)
                    self.webview.load_uri(source_uri(source))
                    self._respond(connection, request_id, {"ok": True, "running": True})
            elif command == "show":
                self.show_login()
                self._respond(connection, request_id, {"ok": True, "running": True})
            elif command == "stop":
                self._respond(connection, request_id, {"ok": True, "running": False})
                self.quit()
            else:
                self._respond(connection, request_id, {"ok": False, "error": "unknown command"})
            return False

        def _secure_collections(self, collections):
            for item in collections:
                remote_art = normalize_list_artwork_url(item.pop("artUrl"))
                item["artworkId"] = ""
                if remote_art:
                    token = hashlib.sha256(remote_art.encode("utf-8")).hexdigest()[:24]
                    self.track_artwork_urls[token] = remote_art
                    self.track_artwork_urls.move_to_end(token)
                    while len(self.track_artwork_urls) > 200:
                        self.track_artwork_urls.popitem(last=False)
                    item["artworkId"] = token
            return collections

        def _load_mixes(self, connection, request_id, attempt):
            """Mixes come from Home's selections; load Home first when the page has none."""
            def needs_home():
                if attempt == 0:
                    if self.page_media_present or self.page_playing:
                        self._respond(connection, request_id, {
                            "ok": False, "running": True, "tracks": [], "collections": [],
                            "error": "Open Home once to load your mixes",
                        })
                        return
                    self.cached_sources.discard("home")
                    self.webview.load_uri(source_uri("home"))
                if attempt >= 15:
                    self._respond(connection, request_id, {
                        "ok": False, "running": True, "tracks": [], "collections": [],
                        "error": "SoundCloud mixes did not load",
                    })
                    return
                GLib.timeout_add(1000, lambda: self._load_mixes(connection, request_id, attempt + 1) and False)

            self._page_list(MIXES_FUNCTION, {}, "", connection, request_id, {}, needs_home)

        def _page_list(self, function, arguments, list_name, connection, request_id, echo, needs_home=None):
            """Run a page list function (search, likes, playlist, artist) and answer with its rows."""
            def respond(payload):
                self._respond(connection, request_id, {"running": True, **echo, **payload})

            def failed(error):
                respond({"ok": False, "tracks": [], "collections": [], "error": error})

            def finished(webview, async_result, _data):
                try:
                    value = webview.call_async_javascript_function_finish(async_result)
                    raw = value.to_string() if value.is_string() else ""
                    if not raw or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
                        failed("SoundCloud request failed")
                        return
                    payload = json.loads(raw)
                except Exception:
                    failed("SoundCloud request failed")
                    return
                if not isinstance(payload, dict):
                    failed("SoundCloud request failed")
                    return
                if needs_home and payload.get("needsHome") is True:
                    needs_home()
                    return
                error = _bounded_string(payload.get("error", ""), 128) or ""
                if payload.get("collections"):
                    collections = validate_collections_payload(payload)
                    if collections is None:
                        failed("SoundCloud returned invalid results")
                        return
                    respond({
                        "ok": not error, "tracks": [],
                        "collections": self._secure_collections(collections),
                        **({"error": error} if error else {}),
                    })
                    return
                validated = validate_tracks_payload({"tracks": payload.get("tracks", [])})
                if validated is None:
                    failed("SoundCloud returned invalid results")
                    return
                for track in validated["tracks"]:
                    playback_id = track.get("playbackId")
                    if is_playback_id(playback_id):
                        self.playback_tracks[playback_id] = track
                while len(self.playback_tracks) > MAX_TRACKS * 4:
                    self.playback_tracks.pop(next(iter(self.playback_tracks)))
                if list_name and not error:
                    self.lists[list_name] = validated["tracks"]
                result = self._secure_tracks(validated) or {"tracks": []}
                respond({
                    "ok": not error, "collections": [], **result,
                    **({"error": error} if error else {}),
                })

            if not self.webview or not is_soundcloud_uri(self.webview.get_uri() or ""):
                failed("SoundCloud is still loading")
                return
            self.webview.call_async_javascript_function(
                function, -1, GLib.Variant("a{sv}", arguments), None, None, None, finished, None
            )

        def _write_like_in_page(self, user_id, track_id, liked, token):
            """Run on a worker thread; blocks until the page's fetch finishes."""
            done = threading.Event()
            result = {"outcome": "failed"}

            def finished(webview, async_result, _data):
                try:
                    value = webview.call_async_javascript_function_finish(async_result)
                    result["outcome"] = value.to_string() if value.is_string() else "failed"
                except Exception:
                    pass
                done.set()

            def start():
                if not self.webview or not is_soundcloud_uri(self.webview.get_uri() or ""):
                    done.set()
                    return False
                arguments = GLib.Variant("a{sv}", {
                    "userId": GLib.Variant("d", user_id),
                    "trackId": GLib.Variant("d", track_id),
                    "method": GLib.Variant("s", "PUT" if liked else "DELETE"),
                    "token": GLib.Variant("s", token),
                })
                self.webview.call_async_javascript_function(
                    LIKE_TRACK_FUNCTION, -1, arguments, None, None, None, finished, None
                )
                return False

            GLib.idle_add(start)
            if not done.wait(15.0):
                raise TimeoutError("like request timed out")
            if result["outcome"] == "bot-check":
                raise BotCheckRequired("SoundCloud asked for a captcha")
            if result["outcome"] != "ok":
                raise OSError("like request failed")

        def _evaluate(self, script, callback):
            if not self.webview or not is_soundcloud_uri(self.webview.get_uri() or ""):
                callback({"error": "refusing untrusted page"})
                return
            def finished(webview, result, _data):
                try:
                    js_result = webview.run_javascript_finish(result)
                    value = js_result.get_js_value()
                    payload = json.loads(value.to_json(0))
                except Exception as exc:
                    payload = {"error": str(exc)}
                callback(payload)

            self.webview.run_javascript(script, None, finished, None)

    return SoundCloudApplication()
