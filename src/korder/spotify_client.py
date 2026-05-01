"""Spotify Web API client — search-only via Client Credentials flow.

Free tier, no user OAuth required, no Premium requirement. Used to convert
voice search queries to specific spotify: URIs that Spotify desktop
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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"

# Picker priority: when a query matches names of multiple types equally
# (exact or substring), prefer in this order. Artist beats album because
# artist names are more uniquely-identifying ("Pink Floyd" the artist vs.
# "Pink Floyd" buried in some compilation track). Album beats track
# because album titles are usually the canonical "named thing" — the
# title track ("Meteora") shares the name but the album is the intent.
_TYPE_PRIORITY = ("artist", "album", "track", "playlist")
_VALID_KINDS = frozenset(_TYPE_PRIORITY)


# Letters that don't decompose under NFKD and need an explicit fold.
# Polish ł is the headline case; ø/æ/ß added so non-Polish band names
# ("Mötley Crüe" → handled by NFKD; "Mötorhead" — fine; "Sigur Rós" — fine;
# "Kælan Mikla" / "Sælig" — handled here).
_NO_DECOMP_FOLDS = str.maketrans({
    "ł": "l", "Ł": "l",
    "ø": "o", "Ø": "o",
    "æ": "ae", "Æ": "ae",
    "ß": "ss",
})


def _normalize(name: str) -> str:
    """Lowercase + strip diacritics + collapse whitespace, for fuzzy name
    comparison. 'Małomiasteczkowy' and 'malomiasteczkowy' compare equal."""
    folded = name.translate(_NO_DECOMP_FOLDS)
    nfkd = unicodedata.normalize("NFKD", folded)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


class SpotifyClient:
    """Caches a client-credentials access token, refreshes when expired."""

    def __init__(self, client_id: str, client_secret: str, timeout_s: float = 5.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout_s = timeout_s
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    def search(self, query: str, kind: str | None = None) -> str | None:
        """Return the top result's spotify: URI for the given kind
        ("album"/"track"/"artist"/"playlist"), or None.

        When `kind` is None or unrecognized, search all four types in one
        request and pick by name-similarity to the query — artist > album
        > track > playlist within each match tier (exact, then substring)."""
        normalized_kind = (kind or "").lower().strip()
        if normalized_kind in _VALID_KINDS:
            return self._search_one(query, normalized_kind)
        return self._search_unspecified(query)

    def _search_one(self, query: str, kind: str) -> str | None:
        if not query.strip():
            return None
        body = self._call_search(query, kind)
        if body is None:
            return None
        items = body.get(f"{kind}s", {}).get("items") or []
        first = items[0] if items else None
        return first.get("uri") if first else None

    def _search_unspecified(self, query: str) -> str | None:
        if not query.strip():
            return None
        body = self._call_search(query, ",".join(_TYPE_PRIORITY))
        if body is None:
            return None
        # Top hit per type (Spotify returns each type's items separately,
        # not a unified ranking — the picker is on us).
        candidates: dict[str, dict] = {}
        for t in _TYPE_PRIORITY:
            items = body.get(f"{t}s", {}).get("items") or []
            # Spotify occasionally returns null entries for playlists when
            # the playlist owner has been deactivated; filter those out.
            for it in items:
                if isinstance(it, dict) and it.get("name") and it.get("uri"):
                    candidates[t] = it
                    break
        if not candidates:
            return None
        nq = _normalize(query)

        # Tier 1: exact normalized name match. Iterate in priority order.
        for t in _TYPE_PRIORITY:
            cand = candidates.get(t)
            if cand and _normalize(cand["name"]) == nq:
                return cand["uri"]
        # Tier 2: query is a substring of the name.
        for t in _TYPE_PRIORITY:
            cand = candidates.get(t)
            if cand and nq in _normalize(cand["name"]):
                return cand["uri"]
        # Tier 3: nothing matched by name. Caller (action layer) will fall
        # back to the open-search-UI path.
        return None

    def _call_search(self, query: str, type_param: str) -> dict | None:
        token = self._get_token()
        if not token:
            return None
        params = urllib.parse.urlencode({"q": query, "type": type_param, "limit": 1})
        url = f"{_SEARCH_URL}?{params}"
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            print(f"[korder] Spotify search ({type_param}) failed: {e}", flush=True)
            return None

    # Backward-compat alias for callers that imported the old name.
    def search_track(self, query: str) -> str | None:
        return self._search_one(query, "track")

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
