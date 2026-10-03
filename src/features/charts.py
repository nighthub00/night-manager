"""
features/charts.py
Roblox Charts (the explore sorts), game search and game icons for the Charts tab.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import threading
import time
import uuid
from typing import Callable

import requests

from classes.operation_result import OperationResult
from utils.app_paths import get_data_dir


_EXPLORE_URL = "https://apis.roblox.com/explore-api/v1/get-sorts"
_SEARCH_URL = "https://apis.roblox.com/search-api/omni-search"
_ICONS_URL = "https://thumbnails.roblox.com/v1/games/icons"
_PLACE_UNIVERSE_URL = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"
_GAMES_URL = "https://games.roblox.com/v1/games"
_VOTES_URL = "https://games.roblox.com/v1/games/votes"

_ICON_CACHE_DIR = os.path.join(get_data_dir(), "game_icon_cache")
_ICON_MAX_AGE = 3 * 24 * 3600
_SORTS_TTL = 5 * 60
_MAX_SORT_PAGES = 6
_SKIPPED_SORTS = {"more-when-you-subscribe"}

# The explore and search APIs reject requests without a session id; one per app run
# matches what the Roblox website sends.
_SESSION_ID = str(uuid.uuid4())
_THREAD_LOCAL = threading.local()
_LOCK = threading.RLock()
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="chart-icon")
_INFLIGHT: set[str] = set()
_sorts_cache: tuple[float, list[dict]] | None = None


def _get_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["Accept"] = "application/json"
        _THREAD_LOCAL.session = session
    return session


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_game(raw: dict) -> dict | None:
    universe_id = _to_int(raw.get("universeId"))
    place_id = _to_int(raw.get("rootPlaceId"))
    if not universe_id or not place_id:
        return None
    return {
        "universe_id": str(universe_id),
        "place_id": str(place_id),
        "name": str(raw.get("name") or place_id),
        "playing": _to_int(raw.get("playerCount")),
        "up_votes": _to_int(raw.get("totalUpVotes")),
        "down_votes": _to_int(raw.get("totalDownVotes")),
        "genre": str(raw.get("genreL1") or raw.get("genre") or ""),
        "maturity": str(raw.get("ageRecommendationDisplayName") or ""),
        "sponsored": bool(raw.get("isSponsored")),
    }


def _normalize_games(raw_games) -> list[dict]:
    games = []
    seen = set()
    for raw in raw_games or []:
        if not isinstance(raw, dict):
            continue
        game = _normalize_game(raw)
        if game is None or game["universe_id"] in seen:
            continue
        seen.add(game["universe_id"])
        games.append(game)
    return games


def _network_failure(what: str, exc: Exception) -> OperationResult:
    return OperationResult.failure(
        "CHARTS_NETWORK",
        "Charts Unavailable",
        f"Could not load {what} from Roblox. Check your connection and press Refresh.",
        detail=str(exc),
        retryable=True,
    )


def fetch_sorts(force: bool = False) -> OperationResult:
    """Every Charts list Roblox serves (Top Trending, Top Playing Now, Trending in RPG, ...)."""
    global _sorts_cache
    with _LOCK:
        cached = _sorts_cache
    if not force and cached and time.time() - cached[0] < _SORTS_TTL:
        return OperationResult.success(data=cached[1])

    sorts: list[dict] = []
    token = ""
    try:
        for _ in range(_MAX_SORT_PAGES):
            params = {"sessionId": _SESSION_ID, "device": "computer", "country": "all"}
            if token:
                params["sortsPageToken"] = token
            response = _get_session().get(_EXPLORE_URL, params=params, timeout=12)
            if response.status_code != 200:
                if sorts:
                    break
                return OperationResult.failure(
                    "CHARTS_HTTP",
                    "Charts Unavailable",
                    f"Roblox answered {response.status_code} for the Charts list.",
                    detail=response.text[:300],
                    retryable=True,
                )
            payload = response.json()
            for sort in payload.get("sorts") or []:
                sort_id = str(sort.get("sortId") or "")
                if sort.get("contentType") != "Games" or sort_id in _SKIPPED_SORTS:
                    continue
                games = _normalize_games(sort.get("games"))
                if games:
                    sorts.append({
                        "id": sort_id,
                        "name": str(sort.get("sortDisplayName") or sort_id),
                        "games": games,
                    })
            token = str(payload.get("nextSortsPageToken") or "")
            if not token:
                break
    except (requests.RequestException, ValueError) as exc:
        if not sorts:
            return _network_failure("the Charts", exc)

    if not sorts:
        return OperationResult.failure(
            "CHARTS_EMPTY", "Charts Unavailable", "Roblox returned no Charts lists."
        )
    with _LOCK:
        _sorts_cache = (time.time(), sorts)
    return OperationResult.success(data=sorts)


def _place_id_from_query(query: str) -> str:
    match = re.search(r"roblox\.com/(?:[a-z]{2}/)?games/(\d+)", query, re.IGNORECASE)
    if match:
        return match.group(1)
    return query if query.isdigit() and len(query) >= 4 else ""


def lookup_place(place_id: str) -> dict | None:
    """Turns a Place ID into a chart-style game row, so pasted IDs and links work in search."""
    session = _get_session()
    response = session.get(_PLACE_UNIVERSE_URL.format(place_id=place_id), timeout=8)
    if response.status_code != 200:
        return None
    universe_id = _to_int(response.json().get("universeId"))
    if not universe_id:
        return None
    response = session.get(_GAMES_URL, params={"universeIds": universe_id}, timeout=8)
    if response.status_code != 200:
        return None
    data = response.json().get("data") or []
    if not data:
        return None
    info = data[0]
    up = down = 0
    try:
        votes = session.get(_VOTES_URL, params={"universeIds": universe_id}, timeout=8)
        if votes.status_code == 200:
            vote = (votes.json().get("data") or [{}])[0]
            up, down = _to_int(vote.get("upVotes")), _to_int(vote.get("downVotes"))
    except (requests.RequestException, ValueError):
        pass
    return _normalize_game({
        "universeId": universe_id,
        "rootPlaceId": info.get("rootPlaceId") or place_id,
        "name": info.get("name"),
        "playerCount": info.get("playing"),
        "totalUpVotes": up,
        "totalDownVotes": down,
        "genreL1": info.get("genre_l1") or info.get("genre"),
    })


def search_games(query: str) -> OperationResult:
    query = query.strip()
    if not query:
        return OperationResult.success(data=[])
    try:
        place_id = _place_id_from_query(query)
        if place_id:
            game = lookup_place(place_id)
            if game:
                return OperationResult.success(data=[game])

        response = _get_session().get(
            _SEARCH_URL,
            params={"searchQuery": query, "sessionId": _SESSION_ID, "pageType": "all"},
            timeout=12,
        )
        if response.status_code != 200:
            return OperationResult.failure(
                "SEARCH_HTTP",
                "Search Failed",
                f"Roblox answered {response.status_code} for the search.",
                detail=response.text[:300],
                retryable=True,
            )
        raw = []
        for group in response.json().get("searchResults") or []:
            if group.get("contentGroupType") == "Game":
                raw.extend(group.get("contents") or [])
    except (requests.RequestException, ValueError) as exc:
        return _network_failure("search results", exc)
    return OperationResult.success(data=_normalize_games(raw))


def _icon_path(universe_id: str) -> str:
    return os.path.join(_ICON_CACHE_DIR, f"{universe_id}.png")


def _load_cached_icon(universe_id: str) -> bytes | None:
    path = _icon_path(universe_id)
    try:
        if time.time() - os.path.getmtime(path) > _ICON_MAX_AGE:
            return None
        with open(path, "rb") as f:
            return f.read() or None
    except OSError:
        return None


def _download_icon(universe_id: str, url: str, on_ready: Callable[[str, bytes], None]) -> None:
    try:
        response = _get_session().get(url, timeout=10)
        if response.status_code == 200 and response.content:
            try:
                os.makedirs(_ICON_CACHE_DIR, exist_ok=True)
                with open(_icon_path(universe_id), "wb") as f:
                    f.write(response.content)
            except OSError:
                pass
            on_ready(universe_id, response.content)
    except requests.RequestException:
        pass
    finally:
        with _LOCK:
            _INFLIGHT.discard(universe_id)


def _resolve_icons(universe_ids: list[str], on_ready: Callable[[str, bytes], None]) -> None:
    missing = []
    for universe_id in universe_ids:
        data = _load_cached_icon(universe_id)
        if data:
            on_ready(universe_id, data)
            with _LOCK:
                _INFLIGHT.discard(universe_id)
        else:
            missing.append(universe_id)

    for start in range(0, len(missing), 100):
        batch = missing[start:start + 100]
        urls: dict[str, str] = {}
        try:
            response = _get_session().get(
                _ICONS_URL,
                params={
                    "universeIds": ",".join(batch),
                    "size": "150x150",
                    "format": "Png",
                    "isCircular": "false",
                },
                timeout=10,
            )
            if response.status_code == 200:
                for item in response.json().get("data") or []:
                    url = str(item.get("imageUrl") or "")
                    if item.get("state") == "Completed" and url:
                        urls[str(item.get("targetId"))] = url
        except (requests.RequestException, ValueError):
            pass
        for universe_id in batch:
            url = urls.get(universe_id)
            if url:
                _EXECUTOR.submit(_download_icon, universe_id, url, on_ready)
            else:
                with _LOCK:
                    _INFLIGHT.discard(universe_id)


def fetch_icons_async(universe_ids: list[str], on_ready: Callable[[str, bytes], None]) -> None:
    """Calls on_ready(universe_id, png_bytes) from a worker thread as each icon arrives."""
    wanted = []
    with _LOCK:
        for universe_id in dict.fromkeys(str(u) for u in universe_ids if u):
            if universe_id not in _INFLIGHT:
                _INFLIGHT.add(universe_id)
                wanted.append(universe_id)
    if wanted:
        _EXECUTOR.submit(_resolve_icons, wanted, on_ready)


def format_count(value: int) -> str:
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            scaled = value / limit
            digits = 2 if scaled < 10 else 1 if scaled < 100 else 0
            text = f"{scaled:.{digits}f}"
            if digits:
                text = text.rstrip("0").rstrip(".")
            return text + suffix
    return str(value)


def rating_percent(game: dict) -> int | None:
    total = game.get("up_votes", 0) + game.get("down_votes", 0)
    if total <= 0:
        return None
    return round(game["up_votes"] * 100 / total)
