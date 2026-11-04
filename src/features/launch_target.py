"""
features/launch_target.py
Turns anything pasted into the Place ID box into something to join: game links, place IDs,
job IDs, private server links (link or access code), roblox.com/share links (both formats),
roblox:// deep links, ro.blox.com links, universes, profiles and @usernames.

`parse` is local and never touches the network; `resolve` asks Roblox (with the launching
account's cookie) about share links and universes. A port of RoALT's launcher/target.rs and
launcher::resolve (MIT, github.com/inspectsoftware/RoALT), checked against its 38 recorded cases.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

import requests

from classes.roblox_api import RobloxAPI, _http

EMPTY = "Paste a game link, an ID or a username."
NOT_ROBLOX = "That isn't a Roblox link."
NOT_GAME = "That link doesn't point to a game."
BAD_PLACE = "That place ID isn't valid."
BAD_USER = "That user ID isn't valid."
BAD_UNIVERSE = "That game ID isn't valid."
BAD_JOB = "That server ID isn't valid."
BAD_LINK_CODE = "That private server link code isn't valid."
BAD_ACCESS_CODE = "That private server access code isn't valid."
BAD_SHARE_CODE = "That share link's code isn't valid."
SHARE_TYPE_MISSING = "That share link is missing its type."
BAD_SHARE_TYPE = "That share link's type isn't known."
BAD_USERNAME = "That username isn't valid."
LAUNCH_URI = "Launch links carry a sign-in ticket and aren't accepted. Paste the game link instead."
SHORT_LINK = "Open that ro.blox.com link in your browser, then paste the roblox.com link it goes to."
NESTED = "That ro.blox.com link wraps another ro.blox.com link."

_WEB_HOSTS = {"roblox.com", "www.roblox.com", "web.roblox.com", "m.roblox.com"}
# The web's share-link types; True = it can lead into a game.
_SHARE_TYPES = {
    "server": ("Server", True),
    "experienceinvite": ("ExperienceInvite", True),
    "notificationexperienceinvite": ("NotificationExperienceInvite", True),
    "screenshotinvite": ("ScreenshotInvite", True),
    "experiencedetails": ("ExperienceDetails", True),
    "experienceaffiliate": ("ExperienceAffiliate", True),
    "experienceevent": ("ExperienceEvent", True),
    "profile": ("Profile", True),
    "friendinvite": ("FriendInvite", False),
    "avataritemdetails": ("AvatarItemDetails", False),
    "avataritemaffiliate": ("AvatarItemAffiliate", False),
    "contentpost": ("ContentPost", False),
}
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_USERNAME = re.compile(r"^[A-Za-z0-9_]{3,20}$")
_CODE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*$")
_LOCALE = re.compile(r"^[a-z]{2}(-[a-z0-9]{2,4})?$")
_I64_MAX = 9223372036854775807


def _invalid(reason: str) -> dict:
    return {"kind": "Invalid", "reason": reason}


def _unsupported(reason: str) -> dict:
    return {"kind": "Unsupported", "reason": reason}


def _place(place_id: int, launch_data: str | None = None) -> dict:
    result = {"kind": "Place", "place_id": place_id}
    if launch_data is not None:
        result["launch_data"] = launch_data
    return result


def _follow(user_id: int) -> dict:
    return {"kind": "FollowUser", "user_id": user_id}


def _id(text: str) -> int | None:
    """A positive id that fits an i64, digits only."""
    if not (1 <= len(text) <= 19) or not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    return value if 0 < value <= _I64_MAX else None


def _digits(text: str, longest: int) -> bool:
    return 1 <= len(text) <= longest and text.isascii() and text.isdigit()


def is_uuid(text: str) -> bool:
    return bool(_UUID.match(text or ""))


def _query(raw: str) -> dict[str, str]:
    # Values are percent-decoded once; '+' stays a literal plus, as in Roblox's own parser.
    # Keys are lower-cased; the first non-empty value wins.
    query: dict[str, str] = {}
    for key, value in parse_qsl(raw.replace("+", "%2B"), keep_blank_values=True):
        if value:
            query.setdefault(key.lower(), value)
    return query


def _first(query: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in query:
            return query[key]
    return None


def parse(text: str) -> dict:
    """What `text` points at, without asking anyone. Never raises."""
    try:
        return _parse_at(text, 0)
    except Exception:
        return _invalid(NOT_ROBLOX)


def _parse_at(text: str, depth: int) -> dict:
    s = (text or "").strip().strip("<>\"'").strip()
    if not s:
        return _invalid(EMPTY)
    if s.isascii() and s.isdigit():
        place_id = _id(s)
        return _place(place_id) if place_id else _invalid(BAD_PLACE)
    if is_uuid(s):
        return {"kind": "JobIdOnly", "job_id": s.lower()}
    if s.startswith("@"):
        name = s[1:]
        return {"kind": "Username", "name": name} if _USERNAME.match(name) else _invalid(BAD_USERNAME)
    if _USERNAME.match(s):
        return {"kind": "Username", "name": s}
    scheme, sep, rest = s.partition(":")
    if not sep or not _SCHEME.match(scheme):
        return _web("https://" + s, depth)
    scheme = scheme.lower()
    if scheme == "roblox" and rest.startswith("//"):
        return _deeplink(rest[2:])
    if scheme in ("roblox-player", "roblox-studio", "roblox-studio-auth"):
        return _invalid(LAUNCH_URI)
    if scheme in ("http", "https"):
        return _web(s, depth)
    return _invalid(NOT_ROBLOX)


def _web(url: str, depth: int) -> dict:
    # Hosts are read the WHATWG way: a backslash ends the host (`evil.com\@www.roblox.com`
    # is evil.com).
    try:
        parts = urlsplit(url.replace("\\", "/"))
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return _invalid(NOT_ROBLOX)
    query = _query(parts.query)
    if host == "ro.blox.com":
        return _apps_flyer(query, depth)
    if host not in _WEB_HOSTS:
        return _invalid(NOT_ROBLOX)
    segments = [segment.lower() for segment in parts.path.split("/") if segment]
    if (
        len(segments) >= 2
        and _LOCALE.match(segments[0])
        and segments[1] in ("games", "share", "share-links", "users")
    ):
        segments.pop(0)
    if segments[:2] == ["games", "start"]:
        return _from_query(query)
    if segments and segments[0] == "games" and len(segments) >= 2:
        place_id = _id(segments[1])
        return _with_place(place_id, query, start=False) if place_id else _invalid(BAD_PLACE)
    if segments in (["share"], ["share-links"]):
        return _share(query)
    if segments and segments[0] == "users" and len(segments) >= 2:
        user_id = _id(segments[1])
        return _follow(user_id) if user_id else _invalid(BAD_USER)
    if segments == ["placeitem.aspx"]:
        place_id = _id(query.get("id", ""))
        return _place(place_id) if place_id else _invalid(BAD_PLACE)
    return _unsupported(NOT_GAME)


def _apps_flyer(query: dict[str, str], depth: int) -> dict:
    # ro.blox.com short links carry the real link in the query; they're never fetched.
    if depth > 0:
        return _invalid(NESTED)
    inner = _first(query, ("af_dp", "deep_link_value", "af_web_dp"))
    return _parse_at(inner, depth + 1) if inner else _unsupported(SHORT_LINK)


def _deeplink(rest: str) -> dict:
    path, _, raw = rest.partition("?")
    # The legacy form roblox://placeId=1&.. has no '?'.
    if not raw and "=" in path:
        path, raw = "", path.rstrip("/")
    query = _query(raw)
    route = path.strip("/").lower()
    if route in ("", "experiences/start"):
        return _from_query(query)
    if route == "navigation/share_links":
        return _share(query)
    if route == "navigation/game_details":
        universe_id = _id(query.get("gameid", ""))
        return {"kind": "Universe", "universe_id": universe_id} if universe_id else _invalid(BAD_UNIVERSE)
    if route == "navigation/profile":
        user_id = _id(query.get("userid", ""))
        return _follow(user_id) if user_id else _invalid(BAD_USER)
    return _unsupported(NOT_GAME)


def _from_query(query: dict[str, str]) -> dict:
    place = _first(query, ("placeid", "id"))
    if place is not None:
        place_id = _id(place)
        return _with_place(place_id, query, start=True) if place_id else _invalid(BAD_PLACE)
    if "userid" in query:
        user_id = _id(query["userid"])
        return _follow(user_id) if user_id else _invalid(BAD_USER)
    return _invalid(BAD_PLACE)


def _with_place(place_id: int, query: dict[str, str], start: bool) -> dict:
    # Link code, then access code, then job id, as the website's own handler. A game page
    # (start=False) takes neither an access code nor launch data.
    link_code = _first(query, ("linkcode", "privateserverlinkcode"))
    if link_code is not None:
        if not _digits(link_code, 64):
            return _invalid(BAD_LINK_CODE)
        return {"kind": "PrivateServerLinkCode", "place_id": place_id, "link_code": link_code}
    if start and "accesscode" in query:
        access = query["accesscode"]
        if not is_uuid(access):
            return _invalid(BAD_ACCESS_CODE)
        return {"kind": "PrivateServerAccessCode", "place_id": place_id, "access_code": access.lower()}
    if "gameinstanceid" in query:
        job = query["gameinstanceid"]
        if not is_uuid(job):
            return _invalid(BAD_JOB)
        return {"kind": "Instance", "place_id": place_id, "job_id": job.lower()}
    return _place(place_id, query.get("launchdata") if start else None)


def _share(query: dict[str, str]) -> dict:
    code = query.get("code", "")
    if not _CODE.match(code):
        return _invalid(BAD_SHARE_CODE)
    if query.get("v", "").lower() == "v2":
        return {"kind": "ShareLinkV2", "code": code}
    link_type = query.get("type")
    if link_type is None:
        return _invalid(SHARE_TYPE_MISSING)
    known = _SHARE_TYPES.get(link_type.lower())
    if known is None:
        return _invalid(BAD_SHARE_TYPE)
    name, leads_to_game = known
    if not leads_to_game:
        return _unsupported(NOT_GAME)
    return {"kind": "ShareLink", "code": code, "link_type": name}


# Resolving with the account's cookie

class ResolveError(Exception):
    pass


def _post_roblox(url: str, body: dict, cookie: str) -> dict:
    headers = {"Cookie": f".ROBLOSECURITY={cookie}", "Content-Type": "application/json"}
    token = RobloxAPI.get_csrf_token(cookie) or ""
    for _ in range(2):
        if token:
            headers["X-CSRF-TOKEN"] = token
        response = _http().post(url, headers=headers, json=body, timeout=10)
        fresh = response.headers.get("x-csrf-token")
        if response.status_code == 403 and fresh and fresh != token:
            token = fresh
            continue
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                return data
        if response.status_code == 401:
            raise ResolveError("Roblox rejected this account's sign-in. Re-add the account.")
        raise ResolveError(f"Roblox couldn't open that link (HTTP {response.status_code}).")
    raise ResolveError("Roblox refused the link request.")


def root_place(universe_id: int) -> int:
    response = _http().get(f"https://games.roblox.com/v1/games?universeIds={universe_id}", timeout=10)
    if response.status_code == 200:
        for game in (response.json() or {}).get("data", []):
            if int(game.get("id", 0)) == int(universe_id) and game.get("rootPlaceId"):
                return int(game["rootPlaceId"])
    raise ResolveError("That game couldn't be found.")


def _status_is(data: dict, status: str) -> bool:
    return str(data.get("status") or "").lower() == status.lower()


def _share_join(link_type: str, data: dict) -> tuple[dict, str]:
    # One *Data object per link type.
    key = {
        "Server": "privateServerInviteData",
        "ExperienceInvite": "experienceInviteData",
        "NotificationExperienceInvite": "notificationExperienceInviteData",
        "ScreenshotInvite": "screenshotInviteData",
        "ExperienceDetails": "experienceDetailsInviteData",
        "ExperienceAffiliate": "experienceAffiliateData",
        "ExperienceEvent": "experienceEventData",
        "Profile": "profileLinkResolutionResponseData",
    }[link_type]
    target = data.get(key) or {}
    if not isinstance(target, dict) or not target:
        raise ResolveError("Roblox didn't say where that link leads.")
    if _status_is(target, "Invalid"):
        raise ResolveError("This share link is no longer valid.")
    warning = "This share link has expired, so Roblox may refuse the join." if _status_is(target, "Expired") else ""
    if link_type == "Profile":
        user_id = int(target.get("userId") or 0)
        if not user_id:
            raise ResolveError("That profile link doesn't name a user.")
        return {"action": "follow", "user_id": user_id}, warning
    place_id = int(target.get("placeId") or 0)
    if not place_id and target.get("universeId"):
        place_id = root_place(int(target["universeId"]))
    if not place_id:
        raise ResolveError("That link doesn't lead into a game.")
    if link_type == "Server":
        code = str(target.get("linkCode") or "")
        if not code:
            raise ResolveError("Roblox didn't give a private server code for that link.")
        return {"action": "private", "place_id": place_id, "private": code}, warning
    instance = str(target.get("instanceId") or "")
    if instance and is_uuid(instance):
        return {"action": "job", "place_id": place_id, "job_id": instance.lower()}, warning
    return {"action": "place", "place_id": place_id}, warning


def resolve(parsed: dict, cookie: str) -> tuple[dict, str]:
    """A parsed target as one thing to do: {"action": "place" | "job" | "private" | "follow" |
    "username", ...}, plus a warning for the user ('' when none). Raises ResolveError."""
    kind = parsed.get("kind")
    if kind in ("Invalid", "Unsupported"):
        raise ResolveError(parsed["reason"])
    if kind == "JobIdOnly":
        raise ResolveError("That's a server ID. Paste the game's link, or use Join > Job ID.")
    if kind == "Username":
        return {"action": "username", "name": parsed["name"]}, ""
    if kind == "FollowUser":
        return {"action": "follow", "user_id": parsed["user_id"]}, ""
    if kind == "Place":
        warning = "The link's launch data was left out." if parsed.get("launch_data") else ""
        return {"action": "place", "place_id": parsed["place_id"]}, warning
    if kind == "Instance":
        return {"action": "job", "place_id": parsed["place_id"], "job_id": parsed["job_id"]}, ""
    if kind == "PrivateServerLinkCode":
        return {"action": "private", "place_id": parsed["place_id"], "private": parsed["link_code"]}, ""
    if kind == "PrivateServerAccessCode":
        return {"action": "private", "place_id": parsed["place_id"], "private": parsed["access_code"]}, ""
    if kind == "Universe":
        return {"action": "place", "place_id": root_place(parsed["universe_id"])}, ""
    try:
        if kind == "ShareLink":
            data = _post_roblox(
                "https://apis.roblox.com/sharelinks/v1/resolve-link",
                {"linkId": parsed["code"], "linkType": parsed["link_type"]},
                cookie,
            )
            return _share_join(parsed["link_type"], data)
        if kind == "ShareLinkV2":
            data = _post_roblox("https://apis.roblox.com/deeplinks/v2/resolve", {"linkId": parsed["code"]}, cookie)
            if _status_is(data, "Invalid"):
                raise ResolveError("This share link is no longer valid.")
            if str(data.get("linkType") or "").lower() != "experience" or not data.get("targetId"):
                raise ResolveError("That link doesn't lead into a game.")
            warning = "This share link has expired, so Roblox may refuse the join." if _status_is(data, "Expired") else ""
            return {"action": "place", "place_id": int(data["targetId"])}, warning
    except requests.RequestException as exc:
        raise ResolveError(f"Roblox couldn't be reached: {type(exc).__name__}") from exc
    raise ResolveError(NOT_GAME)


def describe(parsed: dict) -> str:
    """A short label for the launch panel ('' for plain place IDs)."""
    kind = parsed.get("kind")
    return {
        "Instance": "Server link",
        "PrivateServerLinkCode": "Private server link",
        "PrivateServerAccessCode": "Private server link",
        "ShareLink": f"Share link ({parsed.get('link_type', '')})",
        "ShareLinkV2": "Share link",
        "Universe": "Game link",
        "FollowUser": "Join a player",
        "Username": f"Join @{parsed.get('name', '')}",
        "JobIdOnly": "Server ID",
    }.get(kind, "")
