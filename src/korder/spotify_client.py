"""Spotify Web API client — search-only via Client Credentials flow.

Free tier, no user OAuth required, no Premium requirement. Used to convert
voice search queries to specific spotify:track: URIs that Spotify desktop
can play directly via D-Bus OpenUri.

Setup (one-time, ~2 min):
1. Visit https://developer.spotify.com/dashboard
2. Create a new app (any name; redirect URL not needed for our use)
3. Copy Client ID and Client Secret
4. Add to ~/.config/korderrc:
     [spotify]
     client_id = ...
     client_secret = ...
"""
from __future__ import annotations
import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"


class SpotifyClient:
    """Caches a client-credentials access token, refreshes when expired."""

    def __init__(self, client_id: str, client_secret: str, timeout_s: float = 5.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout_s = timeout_s
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    def search_track(self, query: str) -> str | None:
        """Return the top track's spotify: URI (e.g. spotify:track:XYZ),
        or None if nothing found / API errored."""
        if not query.strip():
            return None
        token = self._get_token()
        if not token:
            return None
        params = urllib.parse.urlencode({"q": query, "type": "track", "limit": 1})
        url = f"{_SEARCH_URL}?{params}"
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            print(f"[korder] Spotify search failed: {e}", flush=True)
            return None
        items = body.get("tracks", {}).get("items", [])
        if not items:
            return None
        return items[0].get("uri")

    def _get_token(self) -> str | None:
        with self._lock:
            now = time.time()
            if self._token and now < self._token_expires_at - 30:
                return self._token
            if not self.client_id or not self.client_secret:
                return None
            creds = f"{self.client_id}:{self.client_secret}".encode("utf-8")
            auth_header = base64.b64encode(creds).decode("ascii")
            try:
                req = urllib.request.Request(
                    _TOKEN_URL,
                    data=b"grant_type=client_credentials",
                    headers={
                        "Authorization": f"Basic {auth_header}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
                print(f"[korder] Spotify token fetch failed: {e}", flush=True)
                return None
            self._token = body.get("access_token")
            self._token_expires_at = now + float(body.get("expires_in", 3600))
            return self._token
