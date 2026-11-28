"""Shared constants for the SoundCloud backend."""
from __future__ import annotations

APP_ID = "com.github.brunosilveira.OmarchySoundCloud"
APP_NAME = "SoundCloud"
START_URI = "https://soundcloud.com/you/likes"
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 524288
MAX_TITLE_BYTES = 512
MAX_ARTIST_BYTES = 256
MAX_URL_BYTES = 2048
MAX_TRACKS = 100
MAX_PLAY_COUNT = 10**12
MAX_TRACK_DURATION_MS = 7 * 24 * 60 * 60 * 1000
MAX_ARTWORK_BYTES = 131072
MAX_ARTWORK_PIXELS = 1024 * 1024
LIST_ARTWORK_BYTES = 32768
LIST_ARTWORK_PIXELS = 512 * 512
LIST_ARTWORK_DIMENSION = 512
MAX_COOKIE_STORE_BYTES = 512 * 1024
MAX_TRACK_CACHE_BYTES = 1024 * 1024
MAX_COOKIES = 256
MAX_CLIENTS = 8
MAX_PENDING_DISPATCHES = 32
MAX_EVENT_LOG_BYTES = 512 * 1024
CLIENT_TIMEOUT_SECONDS = 5.0
CLIENT_IDLE_SECONDS = 60.0
SEND_TIMEOUT_SECONDS = 0.1
HLS_MAX_WORKERS = 2
HLS_OPERATION_SECONDS = 45.0
ARTWORK_HOSTS = {"i1.sndcdn.com"}
MAX_WAVEFORM_BARS = 200
# SoundCloud's bot protection (DataDome) shows its captcha in an iframe from here.
BOT_CHECK_HOSTS = {"geo.captcha-delivery.com"}
# "Continue with Google/Apple/Facebook" opens a popup on these providers.
# Only sign-in popups may navigate here; the main view stays on SoundCloud.
SIGN_IN_PROVIDER_HOSTS = {
    "accounts.google.com",
    "accounts.youtube.com",
    "appleid.apple.com",
    "idmsa.apple.com",
    "www.facebook.com",
    "m.facebook.com",
    "facebook.com",
}
SOUNDCLOUD_WEB_HOSTS = {
    "soundcloud.com",
    "www.soundcloud.com",
    "secure.soundcloud.com",
    "m.soundcloud.com",
}

PAGE_FUNCTION_MISSING = "return JSON.stringify({ tracks: [], collections: [], error: 'SoundCloud is still loading' });"

SEARCH_TRACKS_FUNCTION = f"""
if (typeof window.__omarchySearch !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchySearch(query, kind);
"""

OPEN_COLLECTION_FUNCTION = f"""
if (typeof window.__omarchyOpenCollection !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchyOpenCollection(kind, id);
"""

RELATED_TRACKS_FUNCTION = f"""
if (typeof window.__omarchyRelated !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchyRelated(id);
"""

MIXES_FUNCTION = f"""
if (typeof window.__omarchyMixes !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchyMixes();
"""

LIKED_TRACKS_FUNCTION = f"""
if (typeof window.__omarchyLikes !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchyLikes();
"""

MAX_SEARCH_QUERY_BYTES = 200

SEARCH_KINDS = {"search": "tracks", "search-playlists": "playlists", "search-users": "users"}

QUEUE_SOURCES = ("home", "feed", "likes", "search", "collection")

MAX_QUEUE = 100

PLAYER_STATE_BYTES = 256 * 1024

GSTREAMER_PACKAGE_ELEMENTS = (
    ("gst-plugins-good", (
        "playbin3", "appsrc", "mpegaudioparse", "mpg123audiodec",
        "audioconvert", "audioresample", "autoaudiosink", "hlsdemux2", "souphttpsrc",
    )),
    ("gst-libav", ("avdec_aac",)),
)

BOT_CHECK_MESSAGE = "SoundCloud wants a quick check. Solve it in the SoundCloud window, then tap the heart again."

LIKE_TRACK_FUNCTION = r"""
  if (!Number.isSafeInteger(userId) || userId <= 0 || !Number.isSafeInteger(trackId) || trackId <= 0
      || (method !== 'PUT' && method !== 'DELETE') || typeof token !== 'string') return 'failed';
  const response = await fetch(
    'https://api-v2.soundcloud.com/users/' + userId + '/track_likes/' + trackId,
    {
      method,
      headers: { Authorization: 'OAuth ' + token },
      credentials: 'include',
      cache: 'no-store',
      redirect: 'error'
    }
  );
  if (response.ok) return 'ok';
  if (response.status !== 403) return 'failed';
  try {
    const text = await response.text();
    const url = new URL(text.length <= 4096 ? JSON.parse(text).url : '');
    return url.protocol === 'https:' && url.hostname === 'geo.captcha-delivery.com'
      ? 'bot-check' : 'failed';
  } catch (_) {
    return 'failed';
  }
"""

MPRIS_BUS_NAME = "org.mpris.MediaPlayer2.omarchy_soundcloud"

MPRIS_PATH = "/org/mpris/MediaPlayer2"

MPRIS_ROOT = "org.mpris.MediaPlayer2"

MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"

MPRIS_XML = """<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>"""

MPRIS_ROOT_PROPERTIES = {
    "CanQuit": ("b", False),
    "CanRaise": ("b", True),
    "HasTrackList": ("b", False),
    "Identity": ("s", APP_NAME),
    "DesktopEntry": ("s", APP_ID),
    "SupportedUriSchemes": ("as", []),
    "SupportedMimeTypes": ("as", []),
}
