"""JavaScript snippets injected into the SoundCloud WebKit view."""
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
from .commands import source_uri
from .uri import is_playback_id

def load_more_script(source: str) -> str | None:
    if source not in {"home", "feed"}:
        return None
    return rf"""(() => {{
      if (typeof window.__omarchyLoadMore !== 'function') {{
        return {{ started: false, hasMore: false }};
      }}
      return window.__omarchyLoadMore({json.dumps(source)});
    }})()"""

def api_tracks_script(source: str) -> str | None:
    if source not in {"home", "feed"}:
        return None
    return rf"""(() => {{
      const source = {json.dumps(source)};
      const cache = window.__omarchyApiTrackLists || Object.create(null);
      window.__omarchyApiTrackLists = cache;
      const state = cache[source];
      if (!state || typeof state !== 'object') {{
        return {{ tracks: [], pending: true, error: '' }};
      }}
      const tracks = Array.isArray(state.tracks) ? state.tracks.slice(0, 100) : [];
      return {{
        tracks,
        pending: state.loading === true || state.completed !== true,
        error: typeof state.error === 'string' ? state.error.slice(0, 128) : ''
      }};
    }})()"""

def api_request_capture_script() -> str:
    return r"""(() => {
      const allowedPaths = new Set(['/stream', '/mixed-selections']);
      const cache = window.__omarchyApiTrackLists || Object.create(null);
      window.__omarchyApiTrackLists = cache;
      const publishedSources = new Set();
      const pagination = Object.create(null);
      const xhrRequests = new WeakMap();
      const playbackTracks = new Map();
      let playbackGeneration = 0;
      let latestRequestContext = null;
      // Any authenticated api-v2 request the frontend makes; search only needs
      // its client id and authorization, not a Home/Feed route.
      let latestApiContext = null;
      const isApiV2 = (input) => {
        try {
          const request = input instanceof Request ? input : null;
          const url = new URL(request ? request.url : input, location.href);
          return url.protocol === 'https:' && url.hostname === 'api-v2.soundcloud.com'
            && (!url.port || url.port === '443') && !url.username && !url.password;
        } catch (_) { return false; }
      };
      const routeGenerations = Object.create(null);
      const nextRouteGeneration = (path) => {
        const source = path === '/stream' ? 'feed' : 'home';
        routeGenerations[source] = (routeGenerations[source] || 0) + 1;
        return routeGenerations[source];
      };
      const boundedText = async (response, maximum) => {
        const declared = Number(response.headers && response.headers.get('content-length'));
        if (Number.isFinite(declared) && declared > maximum) throw new Error('response too large');
        if (!response.body || typeof response.body.getReader !== 'function')
          throw new Error('response body unavailable');
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8', { fatal: true });
        let total = 0;
        let text = '';
        try {
          while (true) {
            const part = await reader.read();
            if (part.done) break;
            if (!(part.value instanceof Uint8Array)) throw new Error('invalid response body');
            total += part.value.byteLength;
            if (total > maximum) {
              await reader.cancel();
              throw new Error('response too large');
            }
            text += decoder.decode(part.value, { stream: true });
          }
          text += decoder.decode();
          return text;
        } catch (error) {
          try { await reader.cancel(); } catch (_) {}
          throw error;
        }
      };
      const normalise = (payload, context, playbackTarget = playbackTracks) => {
        const queue = [[payload, 0]];
        const seenObjects = new Set();
        const seenTracks = new Set();
        const tracks = [];
        let visited = 0;
        while (queue.length && visited++ < 3000 && tracks.length < 50) {
          const current = queue.shift();
          const value = current[0];
          const depth = current[1];
          if (!value || typeof value !== 'object' || seenObjects.has(value)) continue;
          seenObjects.add(value);
          const permalink = typeof value.permalink_url === 'string' ? value.permalink_url : '';
          const title = typeof value.title === 'string' ? value.title.trim().slice(0, 512) : '';
          if ((value.kind === undefined || value.kind === 'track') && permalink && title
              && typeof value.duration === 'number' && !seenTracks.has(permalink)) {
            seenTracks.add(permalink);
            const user = value.user && typeof value.user === 'object' ? value.user : {};
            const publisher = value.publisher_metadata && typeof value.publisher_metadata === 'object'
              ? value.publisher_metadata : {};
            const track = {
              title,
              artist: String(user.username || user.full_name || publisher.artist || '').slice(0, 256),
              url: permalink.slice(0, 2048),
              artUrl: String(value.artwork_url || user.avatar_url || '').slice(0, 2048),
              playCount: Number.isSafeInteger(value.playback_count) ? value.playback_count : 0,
              durationMs: Number.isSafeInteger(value.duration) ? value.duration : 0
            };
            tracks.push(track);
            const transcodings = value.media && Array.isArray(value.media.transcodings)
              ? value.media.transcodings : [];
            const playable = transcodings
              .filter((item) => item && item.format && typeof item.url === 'string'
                && item.url.length <= 4096
                && ['hls', 'progressive'].includes(item.format.protocol))
              .sort((left, right) => {
                const score = (item) => item.format.protocol === 'progressive' ? 0
                  : item.format.mime_type === 'audio/mpeg' ? 1 : 2;
                return score(left) - score(right);
              })[0];
            if (context && context.headers instanceof Headers) {
              try {
                const urn = typeof value.urn === 'string'
                    && /^soundcloud:tracks:[0-9]+$/.test(value.urn)
                  ? value.urn
                  : (Number.isSafeInteger(value.id) && value.id > 0
                    ? 'soundcloud:tracks:' + value.id : '');
                const endpoint = playable ? new URL(playable.url)
                  : (urn
                    ? new URL('https://api.soundcloud.com/tracks/soundcloud:tracks:'
                      + urn.substring('soundcloud:tracks:'.length) + '/streams') : null);
                if (endpoint && !playable) {
                  const clientId = context.headers.get('x-client-id')
                    || context.headers.get('x-soundcloud-client-id');
                  if (clientId) endpoint.searchParams.set('client_id', clientId);
                }
                if (endpoint.protocol === 'https:'
                    && ['api-v2.soundcloud.com', 'api.soundcloud.com'].includes(endpoint.hostname)
                    && !endpoint.port && !endpoint.username && !endpoint.password
                    && (endpoint.pathname.startsWith('/media/')
                      || /^\/tracks\/soundcloud:tracks:[0-9]+\/streams$/.test(endpoint.pathname))) {
                  track.playbackId = urn;
                  if (playbackTarget.size >= 200 && !playbackTarget.has(urn)) {
                    playbackTarget.delete(playbackTarget.keys().next().value);
                  }
                  playbackTarget.set(urn, {
                    endpoint: endpoint.href,
                    headers: new Headers(context.headers),
                    credentials: context.credentials,
                    protocol: playable ? playable.format.protocol : '',
                    mimeType: playable ? playable.format.mime_type : '',
                    streams: !playable,
                    waveformUrl: String(value.waveform_url || '').slice(0, 2048),
                    track
                  });
                }
              } catch (_) {}
            }
          }
          if (depth >= 8) continue;
          if (Array.isArray(value)) {
            for (const child of value.slice(0, 100)) queue.push([child, depth + 1]);
          } else {
            for (const key of Object.keys(value).slice(0, 100)) queue.push([value[key], depth + 1]);
          }
        }
        return tracks;
      };
      const homeTrackIds = (payload) => {
        const ids = [];
        const seen = new Set();
        const selections = payload && Array.isArray(payload.collection) ? payload.collection : [];
        for (const selection of selections.slice(0, 50)) {
          const itemState = selection && selection.items && typeof selection.items === 'object'
            ? selection.items : null;
          const items = Array.isArray(itemState) ? itemState
            : (itemState && Array.isArray(itemState.collection) ? itemState.collection : []);
          for (const item of items.slice(0, 50)) {
            const tracks = item && Array.isArray(item.tracks) ? item.tracks : [];
            for (const track of tracks.slice(0, 100)) {
              const id = track && Number.isSafeInteger(track.id) ? track.id : 0;
              if (id > 0 && !seen.has(id)) {
                seen.add(id);
                ids.push(id);
              }
              if (ids.length >= 50) return ids;
            }
          }
        }
        return ids;
      };
      const homeTracks = async (payload, context, playbackTarget = playbackTracks) => {
        const embedded = normalise(payload, context, playbackTarget);
        const ids = homeTrackIds(payload);
        if (!ids.length || !context || !(context.headers instanceof Headers)) return embedded;
        const request = new URL(context.requestUrl);
        if (request.protocol !== 'https:' || request.hostname !== 'api-v2.soundcloud.com'
            || request.port || request.username || request.password) return embedded;
        const clientId = request.searchParams.get('client_id');
        request.pathname = '/tracks';
        request.search = '';
        request.searchParams.set('ids', ids.join(','));
        if (clientId) request.searchParams.set('client_id', clientId);
        const response = await originalFetch(request.href, {
          method: 'GET', headers: context.headers, credentials: context.credentials,
          cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
        });
        if (!response.ok) throw new Error('home tracks unavailable');
        const text = await boundedText(response, 2 * 1024 * 1024);
        if (!text.length) throw new Error('invalid home tracks response');
        const expanded = normalise(JSON.parse(text), context, playbackTarget);
        const merged = new Map(embedded.map((track) => [track.url, track]));
        for (const track of expanded) merged.set(track.url, track);
        return Array.from(merged.values()).slice(0, 50);
      };
      const validNextUrl = (payload, path) => {
        try {
          const value = payload && typeof payload.next_href === 'string' ? payload.next_href : '';
          if (!value || value.length > 4096) return '';
          const url = new URL(value);
          if (url.protocol !== 'https:'
              || url.hostname !== 'api-v2.soundcloud.com'
              || (url.port && url.port !== '443')
              || url.username || url.password
              || url.pathname !== path) return '';
          return url.href;
        } catch (_) { return ''; }
      };
      const requestContext = (input, init) => {
        const request = input instanceof Request ? input : null;
        const headers = new Headers(request ? request.headers : undefined);
        if (init && init.headers) {
          for (const [name, value] of new Headers(init.headers)) headers.set(name, value);
        }
        const privateHeaders = new Headers();
        for (const name of ['authorization', 'x-client-id', 'x-soundcloud-client-id']) {
          const value = headers.get(name);
          if (value && value.length <= 2048) privateHeaders.set(name, value);
        }
        return {
          headers: privateHeaders,
          credentials: String((init && init.credentials) || (request && request.credentials) || 'omit'),
          requestUrl: new URL(request ? request.url : input, location.href).href
        };
      };
      const finishWithoutPage = (source, error, requestUrl, routeGeneration) => {
        if (routeGeneration !== routeGenerations[source]) return;
        const state = pagination[source];
        if (state) {
          state.loading = false;
          if (requestUrl && state.seen instanceof Set) state.seen.delete(requestUrl);
          if (state.lastRequested === requestUrl) state.lastRequested = '';
        }
        const current = cache[source];
        if (current) {
          current.loading = false;
          current.completed = true;
          current.error = String(error || '').slice(0, 128);
        }
        window.webkit.messageHandlers.omarchyTracks.postMessage(JSON.stringify({
          source,
          tracks: [],
          reset: false,
          hasMore: true,
          addedCount: 0,
          error: String(error || 'Could not load more tracks').slice(0, 128)
        }));
      };
      // Home's selections (your mixes, stations, playlists) for the Mixes tab.
      let homeSelections = [];
      const rememberSelections = (payload) => {
        const list = payload && Array.isArray(payload.collection) ? payload.collection.slice(0, 50) : [];
        const known = new Set(homeSelections.map((selection) => selection && selection.urn));
        homeSelections = homeSelections
          .concat(list.filter((selection) => selection && typeof selection === 'object' && !known.has(selection.urn)))
          .slice(0, 50);
      };
      window.__omarchyHomeSelections = () => homeSelections;
      const publish = async (path, text, context, routeGeneration) => {
        try {
          if (!(text.length > 0 && text.length <= 2 * 1024 * 1024)) throw new Error('invalid response');
          const payload = JSON.parse(text);
          if (path === '/mixed-selections') rememberSelections(payload);
          const stagedPlayback = new Map();
          const tracks = path === '/mixed-selections'
            ? await homeTracks(payload, context, stagedPlayback)
            : normalise(payload, context, stagedPlayback);
          const source = path === '/stream' ? 'feed' : 'home';
          if (routeGeneration !== routeGenerations[source]) return;
          for (const [urn, item] of stagedPlayback) {
            if (playbackTracks.size >= 200 && !playbackTracks.has(urn)) {
              playbackTracks.delete(playbackTracks.keys().next().value);
            }
            playbackTracks.set(urn, item);
          }
          const reset = !publishedSources.has(source);
          publishedSources.add(source);
          const previous = !reset && cache[source] && Array.isArray(cache[source].tracks)
            ? cache[source].tracks : [];
          const merged = new Map(previous.map((track) => [track.url, track]));
          for (const track of tracks) merged.set(track.url, track);
          const visibleTracks = Array.from(merged.values()).slice(0, 100);
          const addedCount = reset ? visibleTracks.length
            : Math.max(0, visibleTracks.length - previous.length);
          const candidateNext = validNextUrl(payload, path);
          const seen = context.seen instanceof Set ? context.seen : new Set();
          if (context.requestUrl) seen.add(context.requestUrl);
          const hasMore = visibleTracks.length < 100
            && (reset || addedCount > 0)
            && !!candidateNext
            && !seen.has(candidateNext);
          pagination[source] = {
            next: hasMore ? candidateNext : '',
            path,
            headers: context.headers,
            credentials: context.credentials,
            loading: false,
            lastRequested: String(context.requestUrl || ''),
            seen
          };
          cache[source] = {
            tracks: visibleTracks,
            loading: false,
            completed: true,
            error: ''
          };
          window.webkit.messageHandlers.omarchyTracks.postMessage(
            JSON.stringify({ source, tracks, reset, hasMore, addedCount })
          );
        } catch (_) {
          if (context.requestUrl) {
            finishWithoutPage(
              path === '/stream' ? 'feed' : 'home',
              'Could not load more tracks',
              context.requestUrl,
              routeGeneration
            );
          }
        }
      };
      const routeFor = (input) => {
        try {
          const request = input instanceof Request ? input : null;
          const url = new URL(request ? request.url : input, location.href);
          if (url.protocol !== 'https:'
              || url.hostname !== 'api-v2.soundcloud.com'
              || (url.port && url.port !== '443')
              || url.username || url.password
              || !allowedPaths.has(url.pathname)) return '';
          return url.pathname;
        } catch (_) { return ''; }
      };
      const markLoading = (path) => {
        if (!path) return;
        const source = path === '/stream' ? 'feed' : 'home';
        const previous = cache[source];
        cache[source] = {
          tracks: previous && Array.isArray(previous.tracks) ? previous.tracks.slice(0, 100) : [],
          loading: true,
          completed: false,
          error: ''
        };
      };
      const originalFetch = window.fetch.bind(window);
      const loadWaveform = async (value) => {
        const source = new URL(String(value || '').replace(/\.png$/, '.json'));
        if (source.protocol !== 'https:' || source.hostname !== 'wave.sndcdn.com'
            || source.port || source.username || source.password
            || !/^\/[A-Za-z0-9_-]+\.json$/.test(source.pathname)) return [];
        const response = await originalFetch(source.href, {
          method: 'GET', credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer'
        });
        if (!response.ok) return [];
        const payload = JSON.parse(await boundedText(response, 262144));
        const samples = payload && Array.isArray(payload.samples) ? payload.samples : [];
        const height = Number(payload.height);
        if (!samples.length || !(height > 0)) return [];
        const count = 76;
        const bars = [];
        for (let index = 0; index < count; index++) {
          const start = Math.floor(index * samples.length / count);
          const end = Math.max(start + 1, Math.floor((index + 1) * samples.length / count));
          let peak = 0;
          for (let sample = start; sample < end && sample < samples.length; sample++) {
            const level = Number(samples[sample]);
            if (Number.isFinite(level) && level > peak) peak = level;
          }
          bars.push(Math.min(1, peak / height) * 100);
        }
        // Stretch each track's own range so dense mixes still show shape.
        const lowest = Math.min(...bars);
        const highest = Math.max(...bars);
        if (highest - lowest < 5) return bars.map(Math.round);
        return bars.map((level) => Math.round(20 + (level - lowest) / (highest - lowest) * 80));
      };
      window.__omarchyPlayApiTrack = function(value, requestId) {
        const target = String(value || '');
        if (!/^soundcloud:tracks:[0-9]+$/.test(target)
            || !Number.isSafeInteger(requestId) || requestId < 0) return false;
        const generation = ++playbackGeneration;
        const pageAudio = document.querySelector('audio');
        if (pageAudio && !pageAudio.paused) pageAudio.pause();
        const loadItem = async () => {
          let item = playbackTracks.get(target);
          if (item) return item;
          const waitDeadline = Date.now() + 30000;
          while (!latestRequestContext && !latestApiContext && Date.now() < waitDeadline) {
            if (generation !== playbackGeneration) throw new Error('selection superseded');
            await new Promise((resolve) => setTimeout(resolve, 25));
          }
          const id = target.substring('soundcloud:tracks:'.length);
          const context = latestRequestContext || latestApiContext;
          if (!context) throw new Error('track context unavailable');
          const request = new URL(context.requestUrl);
          if (request.protocol !== 'https:' || request.hostname !== 'api-v2.soundcloud.com'
              || request.port || request.username || request.password)
            throw new Error('track context unavailable');
          const clientId = request.searchParams.get('client_id');
          request.pathname = '/tracks';
          request.search = '';
          request.searchParams.set('ids', id);
          if (clientId) request.searchParams.set('client_id', clientId);
          const response = await originalFetch(request.href, {
            method: 'GET', headers: context.headers, credentials: context.credentials,
            cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
          });
          if (!response.ok) throw new Error('track unavailable');
          const text = await boundedText(response, 2 * 1024 * 1024);
          if (!text.length) throw new Error('invalid track response');
          normalise(JSON.parse(text), context);
          item = playbackTracks.get(target);
          if (!item) throw new Error('track unavailable');
          return item;
        };
        const itemLoaded = loadItem();
        const waveformLoaded = itemLoaded.then((item) => loadWaveform(item.waveformUrl)).catch(() => []);
        itemLoaded.then((item) => originalFetch(item.endpoint, {
          method: 'GET', headers: item.headers, credentials: item.credentials,
          cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
        }).then((response) => {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return boundedText(response, 16384);
        }).then((text) => {
          if (generation !== playbackGeneration) return;
          if (!text || text.length > 16384) throw new Error('invalid stream response');
          const payload = JSON.parse(text);
          let resolved = String(payload.url || '');
          let protocol = item.protocol;
          let mimeType = item.mimeType;
          if (item.streams) {
            if (typeof payload.hls_aac_160_url === 'string') {
              resolved = payload.hls_aac_160_url;
              protocol = 'hls';
              mimeType = 'audio/mp4';
            } else if (typeof payload.hls_aac_96_url === 'string') {
              resolved = payload.hls_aac_96_url;
              protocol = 'hls';
              mimeType = 'audio/mp4';
            } else if (typeof payload.http_mp3_128_url === 'string') {
              resolved = payload.http_mp3_128_url;
              protocol = 'progressive';
              mimeType = 'audio/mpeg';
            } else if (typeof payload.hls_mp3_128_url === 'string') {
              resolved = payload.hls_mp3_128_url;
              protocol = 'hls';
              mimeType = 'audio/mpeg';
            }
          }
          const stream = new URL(resolved);
          const host = stream.hostname.toLowerCase();
          if (stream.protocol !== 'https:' || stream.port || stream.username || stream.password
              || !(host.endsWith('.sndcdn.com') || host.endsWith('.soundcloud.cloud')))
            throw new Error('invalid stream URL');
          // Never hold playback for the waveform: a slow fetch just means no bars.
          let timer;
          const waveformDeadline = new Promise((resolve) => { timer = setTimeout(() => resolve([]), 500); });
          return Promise.race([waveformLoaded, waveformDeadline]).then((waveform) => {
            clearTimeout(timer);
            if (generation !== playbackGeneration) return;
            window.webkit.messageHandlers.omarchyPlayback.postMessage(JSON.stringify({
              ok: true,
              requestId,
              streamUrl: stream.href,
              protocol,
              mimeType,
              track: item.track,
              waveform
            }));
          });
        })).catch((error) => {
          if (generation !== playbackGeneration) return;
          const detail = /^HTTP [0-9]{3}$/.test(String(error && error.message || ''))
            ? ' (' + error.message + ')' : '';
          window.webkit.messageHandlers.omarchyPlayback.postMessage(JSON.stringify({
            ok: false,
            requestId,
            error: 'SoundCloud could not resolve this track' + detail
          }));
        });
        return true;
      };
      window.__omarchyPlaybackTrackCount = () => playbackTracks.size;
      window.fetch = function(input, init) {
        const path = routeFor(input);
        const context = path ? requestContext(input, init) : null;
        const routeGeneration = path ? nextRouteGeneration(path) : 0;
        if (context) latestRequestContext = context;
        else if (isApiV2(input)) {
          const apiContext = requestContext(input, init);
          if (apiContext.headers.has('authorization') || new URL(apiContext.requestUrl).searchParams.has('client_id'))
            latestApiContext = apiContext;
        }
        markLoading(path);
        return originalFetch(input, init).then((response) => {
          if (path && response.ok) {
            boundedText(response.clone(), 2 * 1024 * 1024)
              .then((text) => publish(path, text, context, routeGeneration))
              .catch(() => {
                if (routeGeneration !== routeGenerations[path === '/stream' ? 'feed' : 'home']) return;
                const current = cache[path === '/stream' ? 'feed' : 'home'];
                if (current) current.loading = false;
              });
          } else if (path) {
            if (routeGeneration !== routeGenerations[path === '/stream' ? 'feed' : 'home']) return response;
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }
          return response;
        }).catch((error) => {
          if (path) {
            if (routeGeneration !== routeGenerations[path === '/stream' ? 'feed' : 'home']) throw error;
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }
          throw error;
        });
      };
      const originalOpen = XMLHttpRequest.prototype.open;
      const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
      const originalSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url) {
        const path = routeFor(url);
        xhrRequests.set(this, {
          path,
          api: !path && isApiV2(url),
          headers: new Headers(),
          credentials: 'include',
          requestUrl: (path || isApiV2(url)) ? new URL(url, location.href).href : ''
        });
        return originalOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        const context = xhrRequests.get(this);
        if (context && (context.path || context.api)
            && /^(authorization|x-client-id|x-soundcloud-client-id)$/i.test(String(name))) {
          const bounded = String(value);
          if (bounded.length <= 2048) context.headers.set(String(name), bounded);
        }
        return originalSetRequestHeader.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function() {
        const context = xhrRequests.get(this) || { path: '', headers: new Headers(), credentials: 'omit' };
        const path = context.path;
        const routeGeneration = path ? nextRouteGeneration(path) : 0;
        if (path) latestRequestContext = context;
        else if (context.api) latestApiContext = context;
        markLoading(path);
        if (path) this.addEventListener('load', function() {
          if (this.status >= 200 && this.status < 300
              && (!this.responseType || this.responseType === 'text')) {
            const text = this.responseText;
            if (typeof text === 'string' && text.length <= 2 * 1024 * 1024)
              publish(path, text, context, routeGeneration);
          } else {
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }
        }, { once: true });
        for (const eventName of ['error', 'abort', 'timeout']) {
          if (path) this.addEventListener(eventName, function() {
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }, { once: true });
        }
        return originalSend.apply(this, arguments);
      };
      window.__omarchyLoadMore = function(source) {
        const expectedPath = source === 'feed' ? '/stream'
          : (source === 'home' ? '/mixed-selections' : '');
        const state = pagination[source];
        if (!expectedPath || !state || state.path !== expectedPath || !state.next) {
          return { started: false, hasMore: false };
        }
        if (state.loading) return { started: false, hasMore: true };
        const requestUrl = state.next;
        const seen = state.seen instanceof Set ? state.seen : new Set();
        if (seen.has(requestUrl) || seen.size >= 128) {
          state.next = '';
          return { started: false, hasMore: false };
        }
        seen.add(requestUrl);
        state.seen = seen;
        state.loading = true;
        state.lastRequested = requestUrl;
        markLoading(expectedPath);
        const routeGeneration = nextRouteGeneration(expectedPath);
        originalFetch(requestUrl, {
          method: 'GET',
          headers: state.headers,
          credentials: state.credentials
        }).then((response) => {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return boundedText(response, 2 * 1024 * 1024);
        }).then((text) => publish(expectedPath, text, {
          headers: state.headers,
          credentials: state.credentials,
          requestUrl,
          seen
        }, routeGeneration)).catch(() => finishWithoutPage(
          source, 'Could not load more tracks', requestUrl, routeGeneration
        ));
        return { started: true, hasMore: true };
      };
      // Track SoundCloud's own web player so the backend can keep only one
      // player audible and pause the page when its window closes.
      const pageMedia = new Set();
      if (typeof HTMLMediaElement !== 'undefined' && HTMLMediaElement.prototype
          && typeof HTMLMediaElement.prototype.play === 'function') {
        const originalPlay = HTMLMediaElement.prototype.play;
        HTMLMediaElement.prototype.play = function() {
          try {
            if (pageMedia.size >= 16 && !pageMedia.has(this)) pageMedia.clear();
            pageMedia.add(this);
            if (typeof window.__omarchyVolume === 'number') this.volume = window.__omarchyVolume;
            window.webkit.messageHandlers.omarchyPlayback.postMessage(
              JSON.stringify({ event: 'page-play' }));
          } catch (_) {}
          return originalPlay.apply(this, arguments);
        };
      }
      const pageMediaElements = () => {
        const media = new Set(pageMedia);
        try {
          for (const node of document.querySelectorAll('audio, video')) media.add(node);
        } catch (_) {}
        return media;
      };
      window.__omarchyPagePlaying = function() {
        for (const item of pageMediaElements()) {
          try { if (!item.paused && !item.ended) return true; } catch (_) {}
        }
        return false;
      };
      // SoundCloud plays through media elements it never attaches to the DOM.
      window.__omarchyPageMediaState = function() {
        let chosen = null;
        for (const item of pageMediaElements()) {
          try {
            if (!item.paused && !item.ended) { chosen = item; break; }
            chosen = item;
          } catch (_) {}
        }
        if (!chosen) return { present: false, playing: false, duration: 0, position: 0 };
        const duration = Number(chosen.duration);
        const position = Number(chosen.currentTime);
        return {
          present: true,
          playing: !chosen.paused && !chosen.ended,
          duration: Number.isFinite(duration) && duration > 0 ? duration : 0,
          position: Number.isFinite(position) && position > 0 ? position : 0
        };
      };
      window.__omarchyPausePage = function() {
        if (!window.__omarchyPagePlaying()) return false;
        try {
          const control = document.querySelector('.playControls__play.playing, .playControl.playing');
          if (control) control.click();
        } catch (_) {}
        for (const item of pageMediaElements()) {
          try { if (!item.paused) item.pause(); } catch (_) {}
        }
        return true;
      };
      window.__omarchySetVolume = function(value) {
        const volume = Math.max(0, Math.min(1, Number(value)));
        if (!Number.isFinite(volume)) return false;
        window.__omarchyVolume = volume;
        for (const item of pageMediaElements()) {
          try { item.volume = volume; } catch (_) {}
        }
        return true;
      };
      const apiGet = async (pathname, params) => {
        const context = latestRequestContext || latestApiContext;
        if (!context || !(context.headers instanceof Headers))
          throw new Error('SoundCloud is still loading, try again in a moment');
        const request = new URL(context.requestUrl);
        if (request.protocol !== 'https:' || request.hostname !== 'api-v2.soundcloud.com'
            || request.port || request.username || request.password)
          throw new Error('SoundCloud is unavailable');
        const clientId = request.searchParams.get('client_id');
        request.pathname = pathname;
        request.search = '';
        for (const [name, value] of Object.entries(params || {})) request.searchParams.set(name, String(value));
        if (clientId) request.searchParams.set('client_id', clientId);
        const response = await originalFetch(request.href, {
          method: 'GET', headers: context.headers, credentials: context.credentials,
          cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
        });
        if (!response.ok) throw new Error('SoundCloud request failed (HTTP ' + response.status + ')');
        const text = await boundedText(response, 2 * 1024 * 1024);
        if (!text.length) throw new Error('SoundCloud returned nothing');
        return { payload: JSON.parse(text), context };
      };
      const failure = (error) => {
        const message = String(error && error.message || '');
        return /^SoundCloud /.test(message) ? message.slice(0, 128) : 'SoundCloud request failed';
      };
      // normalise() walks a bounded number of objects; scan each result on its
      // own so long responses are not cut short.
      const normaliseEach = (payload, context) => {
        const items = Array.isArray(payload) ? payload
          : (payload && Array.isArray(payload.collection) ? payload.collection : [payload]);
        const tracks = [];
        const seen = new Set();
        for (const item of items.slice(0, 100)) {
          for (const track of normalise(item, context)) {
            if (seen.has(track.url)) continue;
            seen.add(track.url);
            tracks.push(track);
          }
          if (tracks.length >= 50) break;
        }
        return tracks.slice(0, 50);
      };
      // Follow SoundCloud's next_href (same api-v2 path only) until 50 tracks.
      const pagedTracks = async (pathname, params, limit = 50) => {
        let { payload, context } = await apiGet(pathname, params);
        let tracks = normaliseEach(payload, context);
        for (let page = 1; page < 8 && tracks.length < limit; page++) {
          const next = validNextUrl(payload, pathname);
          if (!next) break;
          const url = new URL(next);
          const nextParams = {};
          for (const [name, value] of url.searchParams) if (name !== 'client_id') nextParams[name] = value;
          ({ payload, context } = await apiGet(pathname, nextParams));
          const known = new Set(tracks.map((track) => track.url));
          tracks = tracks.concat(normaliseEach(payload, context).filter((track) => !known.has(track.url)));
        }
        return tracks.slice(0, limit);
      };
      // Track stubs ({id}) to full tracks, kept in the given order.
      const tracksForIds = async (ids, known = []) => {
        let tracks = known;
        const have = new Set(tracks.map((track) => track.playbackId));
        const missing = ids.filter((trackId) => !have.has('soundcloud:tracks:' + trackId));
        for (let index = 0; index < missing.length && index < 100; index += 50) {
          const page = await apiGet('/tracks', { ids: missing.slice(index, index + 50).join(',') });
          tracks = tracks.concat(normaliseEach(page.payload, page.context));
        }
        return tracksInOrder(ids, tracks).slice(0, 100);
      };
      // Mixes come from Home's selections; a mix is opened by its row number.
      const mixTrackIds = new Map();
      const selectionRank = (selection) => {
        const urn = String(selection && selection.urn || '');
        const order = ['your-moods', 'personalized-tracks', 'artist-stations', 'recently-played'];
        const rank = order.findIndex((name) => urn.includes(':' + name));
        return rank < 0 ? order.length : rank;
      };
      window.__omarchyMixes = async function() {
        if (!homeSelections.length) return JSON.stringify({ collections: [], needsHome: true });
        mixTrackIds.clear();
        const collections = [];
        const seen = new Set();
        const selections = homeSelections.slice().sort((a, b) => selectionRank(a) - selectionRank(b));
        for (const selection of selections) {
          const itemState = selection.items && typeof selection.items === 'object' ? selection.items : null;
          const items = Array.isArray(itemState) ? itemState
            : (itemState && Array.isArray(itemState.collection) ? itemState.collection : []);
          const group = String(selection.title || '').slice(0, 128);
          for (const item of items.slice(0, 30)) {
            if (!item || typeof item !== 'object' || collections.length >= 50) continue;
            const key = String(item.urn || item.id || '');
            if (!key || seen.has(key)) continue;
            const user = item.user && typeof item.user === 'object' ? item.user : {};
            const art = String(item.calculated_artwork_url || item.artwork_url || user.avatar_url || '').slice(0, 2048);
            const link = typeof item.permalink_url === 'string' && /^https:\/\/soundcloud\.com\//.test(item.permalink_url)
              ? item.permalink_url.slice(0, 2048) : 'https://soundcloud.com/discover';
            if (item.kind === 'system-playlist' && Array.isArray(item.tracks)) {
              const ids = item.tracks.slice(0, 100).map((entry) => positiveId(entry && entry.id)).filter(Boolean);
              if (!ids.length) continue;
              seen.add(key);
              mixTrackIds.set(collections.length + 1, ids);
              collections.push({
                kind: 'mix', id: collections.length + 1,
                title: String(item.title || item.short_title || 'Mix').slice(0, 512),
                subtitle: [group, ids.length + ' tracks'].filter(Boolean).join('  •  ').slice(0, 256),
                artUrl: art, url: link
              });
            } else if (item.kind === 'playlist' && positiveId(item.id)) {
              seen.add(key);
              const count = positiveId(item.track_count);
              collections.push({
                kind: 'playlist', id: item.id,
                title: String(item.title || '').slice(0, 512) || 'Playlist',
                subtitle: [group, count ? count + ' tracks' : ''].filter(Boolean).join('  •  ').slice(0, 256),
                artUrl: art, url: link
              });
            }
          }
        }
        return JSON.stringify({ collections });
      };
      const positiveId = (value) => Number.isSafeInteger(value) && value > 0 ? value : 0;
      const tracksInOrder = (ids, tracks) => {
        const byUrn = new Map(tracks.filter((track) => track.playbackId).map((track) => [track.playbackId, track]));
        const ordered = [];
        for (const id of ids) {
          const track = byUrn.get('soundcloud:tracks:' + id);
          if (track) { ordered.push(track); byUrn.delete('soundcloud:tracks:' + id); }
        }
        return ordered.concat(Array.from(byUrn.values()));
      };
      window.__omarchySearch = async function(value, kind) {
        const query = String(value || '').trim().slice(0, 200);
        const type = ['tracks', 'playlists', 'users'].includes(kind) ? kind : 'tracks';
        if (!query) return JSON.stringify({ tracks: [], collections: [] });
        try {
          const { payload, context } = await apiGet('/search/' + type, { q: query, limit: 40 });
          if (type === 'tracks')
            return JSON.stringify({ tracks: normaliseEach(payload, context) });
          const items = payload && Array.isArray(payload.collection) ? payload.collection.slice(0, 40) : [];
          const collections = [];
          for (const item of items) {
            if (!item || typeof item !== 'object' || !positiveId(item.id)) continue;
            const user = item.user && typeof item.user === 'object' ? item.user : {};
            if (type === 'playlists' && (item.kind === 'playlist' || item.kind === undefined)) {
              const count = positiveId(item.track_count);
              collections.push({
                kind: 'playlist', id: item.id,
                title: String(item.title || '').slice(0, 512),
                subtitle: [String(user.username || ''), count ? count + ' tracks' : ''].filter(Boolean).join('  •  ').slice(0, 256),
                artUrl: String(item.artwork_url || user.avatar_url || '').slice(0, 2048),
                url: String(item.permalink_url || '').slice(0, 2048)
              });
            } else if (type === 'users' && (item.kind === 'user' || item.kind === undefined)) {
              const count = positiveId(item.track_count);
              collections.push({
                kind: 'user', id: item.id,
                title: String(item.username || item.full_name || '').slice(0, 512),
                subtitle: (count ? count + ' tracks' : 'Artist').slice(0, 256),
                artUrl: String(item.avatar_url || '').slice(0, 2048),
                url: String(item.permalink_url || '').slice(0, 2048)
              });
            }
          }
          return JSON.stringify({ collections });
        } catch (error) {
          return JSON.stringify({ tracks: [], collections: [], error: failure(error) });
        }
      };
      window.__omarchyOpenCollection = async function(kind, value) {
        const id = positiveId(Number(value));
        if (!id || !['playlist', 'user', 'mix'].includes(kind))
          return JSON.stringify({ tracks: [], error: 'SoundCloud could not open this' });
        try {
          if (kind === 'mix') {
            const ids = mixTrackIds.get(id);
            if (!ids) throw new Error('SoundCloud mixes changed, open Mixes again');
            return JSON.stringify({ tracks: await tracksForIds(ids) });
          }
          if (kind === 'user') {
            return JSON.stringify({ tracks: await pagedTracks('/users/' + id + '/tracks', { limit: 50 }) });
          }
          const { payload, context } = await apiGet('/playlists/' + id, {});
          const entries = payload && Array.isArray(payload.tracks) ? payload.tracks.slice(0, 100) : [];
          const ids = entries.map((entry) => positiveId(entry && entry.id)).filter(Boolean);
          const embedded = normaliseEach({ collection: entries.filter((entry) => entry && entry.title) }, context);
          return JSON.stringify({ tracks: await tracksForIds(ids, embedded) });
        } catch (error) {
          return JSON.stringify({ tracks: [], error: failure(error) });
        }
      };
      // Tracks SoundCloud relates to one track, for autoplay after a list ends.
      window.__omarchyRelated = async function(value) {
        const id = positiveId(Number(value));
        if (!id) return JSON.stringify({ tracks: [], error: 'SoundCloud could not find related tracks' });
        try {
          const { payload, context } = await apiGet('/tracks/' + id + '/related', { limit: 20 });
          return JSON.stringify({ tracks: normaliseEach(payload, context).slice(0, 20) });
        } catch (error) {
          return JSON.stringify({ tracks: [], error: failure(error) });
        }
      };
      window.__omarchyLikes = async function() {
        try {
          const me = await apiGet('/me', {});
          const userId = positiveId(me.payload && me.payload.id);
          if (!userId) throw new Error('SoundCloud did not return your account');
          return JSON.stringify({ tracks: await pagedTracks('/users/' + userId + '/track_likes', { limit: 50 }, 100) });
        } catch (error) {
          return JSON.stringify({ tracks: [], error: failure(error) });
        }
      };
    })()"""

def api_observation_script() -> str:
    return r"""(() => {
      const paths = [];
      for (const name of performance.getEntriesByType('resource').slice(-500).map((entry) => entry.name)) {
        try {
          const candidate = new URL(name);
          if (candidate.hostname === 'api-v2.soundcloud.com' && paths.indexOf(candidate.pathname) < 0) {
            paths.push(candidate.pathname);
          }
        } catch (_) {}
        if (paths.length >= 32) break;
      }
      const cache = window.__omarchyApiTrackLists || Object.create(null);
      const lists = Object.create(null);
      for (const source of ['home', 'feed']) {
        const state = cache[source];
        if (!state) continue;
        lists[source] = {
          count: Array.isArray(state.tracks) ? state.tracks.length : 0,
          loading: state.loading === true,
          error: String(state.error || '').slice(0, 128)
        };
      }
      return {
        paths: paths.slice(0, 32),
        lists,
        playbackCount: typeof window.__omarchyPlaybackTrackCount === 'function'
          ? Number(window.__omarchyPlaybackTrackCount()) : 0
      };
    })()"""

def play_track_script(playback_id: str, request_id: int) -> str | None:
    if (not is_playback_id(playback_id) or isinstance(request_id, bool)
            or not isinstance(request_id, int) or not 0 <= request_id <= 2**31 - 1):
        return None
    target = json.dumps(playback_id)
    return rf"""(() => {{
      const playbackId = {target};
      return typeof window.__omarchyPlayApiTrack === 'function'
        && window.__omarchyPlayApiTrack(playbackId, {request_id});
    }})()"""

def status_script() -> str:
    return r"""(() => {
      const audio = document.querySelector('audio');
      const hooked = typeof window.__omarchyPageMediaState === 'function'
        ? window.__omarchyPageMediaState() : { present: false, playing: false, duration: 0, position: 0 };
      const playerPresent = !!audio || hooked.present === true;
      const playControl = document.querySelector('.playControls__play, .playControl');
      const controlSaysPause = !!playControl && (
        playControl.classList.contains('playing') ||
        /Pause/i.test(playControl.getAttribute('title') || '') ||
        /Pause/i.test(playControl.getAttribute('aria-label') || '')
      );
      const text = (...selectors) => {
        for (const selector of selectors) {
          const node = document.querySelector(selector);
          if (node && node.textContent.trim()) return node.textContent.trim().slice(0, 512);
        }
        return '';
      };
      const parseClock = (value) => {
        const parts = String(value || '').trim().split(':').map(Number);
        if (!parts.length || parts.some((part) => !Number.isFinite(part))) return 0;
        return parts.reduce((total, part) => total * 60 + part, 0);
      };
      const timelinePosition = parseClock(text(
        '.playbackTimeline__timePassed span[aria-hidden="true"]',
        '.playbackTimeline__timePassed'
      ));
      const timelineDuration = parseClock(text(
        '.playbackTimeline__duration span[aria-hidden="true"]',
        '.playbackTimeline__duration'
      ));
      let artUrl = '';
      for (const selector of [
        '.playbackSoundBadge__avatar img',
        '.playbackSoundBadge__avatar [style*="background-image"]',
        '.playbackSoundBadge__avatar .sc-artwork',
        '.playbackSoundBadge__avatar'
      ]) {
        const art = document.querySelector(selector);
        if (!art) continue;
        if (art.src) { artUrl = art.src.slice(0, 2048); break; }
        const background = art.style.backgroundImage || getComputedStyle(art).backgroundImage || '';
        const match = background.match(/url\(["']?(.*?)["']?\)/);
        if (match) { artUrl = match[1].slice(0, 2048); break; }
      }
      const pagePlaying = (!!audio && !audio.paused) || hooked.playing === true;
      return {
        playerPresent,
        pagePlaying,
        playing: playerPresent ? pagePlaying : false,
        title: playerPresent
          ? text('.playbackSoundBadge__titleLink span[aria-hidden="true"]', '.playbackSoundBadge__titleLink') : '',
        artist: playerPresent
          ? text('.playbackSoundBadge__lightLink', '.playbackSoundBadge__titleContextContainer a') : '',
        artUrl: playerPresent ? artUrl : '',
        loggedIn: !/^\/(signin|register)/.test(location.pathname) &&
          !!document.querySelector('.header__userNavButton, a[href*="/you/library"]'),
        url: location.href,
        duration: audio && isFinite(audio.duration) ? audio.duration
          : (playerPresent ? (hooked.duration || timelineDuration) : 0),
        position: audio && isFinite(audio.currentTime) ? audio.currentTime
          : (playerPresent ? (hooked.position || timelinePosition) : 0)
      };
    })()"""

def command_script(action: str) -> str | None:
    scripts = {
        "play-pause": r"""(() => {
          const button = document.querySelector(
            '.playControls__play, .playControl, button[title="Play"], button[title="Pause"], button[aria-label="Play"], button[aria-label="Pause"]'
          );
          if (button) { button.click(); return true; }
          const audio = document.querySelector('audio');
          if (!audio) return false;
          if (audio.paused) { audio.play(); } else { audio.pause(); }
          return true;
        })()""",
        "next": r"""(() => {
          const button = document.querySelector('.skipControl__next, button[title="Next"], button[aria-label="Next"]');
          if (!button) return false; button.click(); return true;
        })()""",
        "previous": r"""(() => {
          const button = document.querySelector('.skipControl__previous, button[title="Previous"], button[aria-label="Previous"]');
          if (!button) return false; button.click(); return true;
        })()""",
        "pause-page": r"""(() => {
          if (typeof window.__omarchyPausePage === 'function') window.__omarchyPausePage();
          return true;
        })()""",
    }
    return scripts.get(action)

def volume_script(volume: float) -> str:
    return rf"""(() => typeof window.__omarchySetVolume === 'function'
      && window.__omarchySetVolume({max(0.0, min(1.0, float(volume)))!r}))()"""

def seek_script(ratio: float) -> str:
    clamped = max(0.0, min(1.0, float(ratio)))
    return rf"""(() => {{
      const ratio = {clamped!r};
      const audio = document.querySelector('audio');
      if (audio && isFinite(audio.duration) && audio.duration > 0) {{
        audio.currentTime = audio.duration * ratio;
        return true;
      }}
      const timeline = document.querySelector(
        '.playbackTimeline__progressWrapper, .playbackTimeline__progressBackground, [role="slider"]'
      );
      if (!timeline) return false;
      const bounds = timeline.getBoundingClientRect();
      const clientX = bounds.left + bounds.width * ratio;
      const eventOptions = {{ bubbles: true, cancelable: true, clientX, clientY: bounds.top + bounds.height / 2 }};
      for (const type of ['mousedown', 'mouseup', 'click'])
        timeline.dispatchEvent(new MouseEvent(type, eventOptions));
      return true;
    }})()"""
