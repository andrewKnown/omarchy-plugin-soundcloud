"""URI policy helpers and Hyprland/WebKit graphics workarounds."""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse, urlunparse

from .backend_constants import (
    ARTWORK_HOSTS,
    BOT_CHECK_HOSTS,
    MAX_URL_BYTES,
    SIGN_IN_PROVIDER_HOSTS,
    SOUNDCLOUD_WEB_HOSTS,
)

def is_soundcloud_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    try:
        if parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return host in SOUNDCLOUD_WEB_HOSTS

def is_bot_check_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") in BOT_CHECK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )

def is_sign_in_provider_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return False
    host = parsed.hostname.lower().rstrip(".")
    # Google bounces through its country domains to set cookies.
    return host in SIGN_IN_PROVIDER_HOSTS or re.fullmatch(r"accounts\.google(\.com?)?\.[a-z]{2,3}", host) is not None

def is_playback_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"soundcloud:tracks:[1-9][0-9]*", value) is not None

def is_allowed_artwork_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") in ARTWORK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and len(uri.encode("utf-8")) <= MAX_URL_BYTES
        and not any(ord(character) < 32 for character in uri)
    )

def normalize_list_artwork_url(uri: object, size: str = "t120x120") -> str:
    if not isinstance(uri, str) or not is_allowed_artwork_uri(uri):
        return ""
    if not re.fullmatch(r"t\d{2,3}x\d{2,3}", size):
        return ""
    parsed = urlparse(uri)
    path = re.sub(
        r"-(?:large|original|t\d+x\d+)(\.(?:jpe?g|png))$",
        rf"-{size}\1",
        parsed.path,
        flags=re.IGNORECASE,
    )
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))

def nvidia_driver_loaded() -> bool:
    """True when the proprietary NVIDIA kernel module is loaded.

    The open NVIDIA kernel module exposes the same path. Nouveau does not,
    and it does not honor NVIDIA's explicit-sync switch.
    """
    return os.path.exists("/sys/module/nvidia")

def apply_graphics_workarounds(env: dict[str, str] | None = None) -> bool:
    """Keep the WebKitGTK sign-in view painted under Hyprland.

    WebKit's DMA-BUF renderer commits explicit sync without an acquire point.
    Hyprland treats that as a protocol error. On NVIDIA, disabling the
    DMA-BUF renderer avoids the error but draws a blank white window with
    current drivers, so explicit sync is turned off and DMA-BUF stays on.
    An explicit WEBKIT_DISABLE_DMABUF_RENDERER in the environment is left
    untouched. Other drivers still take the DMA-BUF workaround.
    """
    values = os.environ if env is None else env
    if not values.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return False
    changed = False
    if nvidia_driver_loaded():
        if values.get("__NV_DISABLE_EXPLICIT_SYNC") != "1":
            values["__NV_DISABLE_EXPLICIT_SYNC"] = "1"
            changed = True
        return changed
    if "WEBKIT_DISABLE_DMABUF_RENDERER" in values:
        return False
    values["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
    return True
