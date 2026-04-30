"""
Run Logger — Tractive → GPX → Strava (Flask).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import xml.sax.saxutils as xml_esc
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import requests
from flask import (
    Flask,
    current_app,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    session,
    url_for,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TRACTIVE_TOKEN_URL = "https://graph.tractive.com/3/auth/token"
# Default app id used by Tractive mobile clients (override via TRACTIVE_CLIENT_ID if Tractive blocks it).
TRACTIVE_APP_CLIENT_ID_DEFAULT = "625e533dc3c3b41c28a669f0"


def _tractive_app_client_id() -> str:
    return (os.environ.get("TRACTIVE_CLIENT_ID") or TRACTIVE_APP_CLIENT_ID_DEFAULT).strip()


def _tractive_token_request_headers() -> dict[str, str]:
    # Same shape as pytractive base_headers() for auth POST.
    return {
        "x-tractive-client": _tractive_app_client_id(),
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
    }


def _tractive_graph_headers(bearer_token: str, user_id: str) -> dict[str, str]:
    """Headers for authenticated Graph API calls (token + client + user), matching pytractive."""
    h = {
        "Authorization": f"Bearer {bearer_token}",
        "x-tractive-client": _tractive_app_client_id(),
        "Accept": "application/json, text/plain, */*",
    }
    if user_id:
        h["x-tractive-user"] = user_id
    return h
# Bump when you need to confirm Railway deployed this revision (see GET /api/version).
RUNKIKI_BUILD_ID = "2026-04-30.11"
# Tractive /positions expects these query params (see aiotractive tracker.positions).
TRACTIVE_POSITIONS_FORMAT_DEFAULT = "json_segments"
STRAVA_OAUTH = "https://www.strava.com/oauth"
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_TOKENS_PATH = "/tmp/strava_tokens.json"
MAX_PHOTO_BYTES = 50 * 1024 * 1024
UPLOAD_POLL_MAX_SEC = 10.0
UPLOAD_POLL_INTERVAL = 1.0
MIN_TRK_PTS = 2

# ---------------------------------------------------------------------------
# Tractive
# ---------------------------------------------------------------------------


def _tractive_auth_post(email: str, password: str, *, legacy_json_body: bool) -> requests.Response:
    """Primary = pytractive-style (x-tractive-client + slim JSON). Legacy = client_id inside JSON only."""
    body: dict[str, str] = {
        "grant_type": "tractive",
        "platform_email": email,
        "platform_token": password,
    }
    if legacy_json_body:
        body["client_id"] = _tractive_app_client_id()
        return requests.post(
            TRACTIVE_TOKEN_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=30,
        )
    return requests.post(
        TRACTIVE_TOKEN_URL,
        json=body,
        headers=_tractive_token_request_headers(),
        timeout=30,
    )


def _tractive_auth_failed_message(primary: requests.Response, secondary: requests.Response | None) -> str:
    def one(res: requests.Response) -> str:
        try:
            err = res.json()
            if isinstance(err, dict):
                d = err.get("message") or err.get("error") or err.get("description")
                if d:
                    return f"HTTP {res.status_code}: {d}"
            elif isinstance(err, str) and err.strip():
                return f"HTTP {res.status_code}: {err.strip()}"
        except Exception:
            pass
        t = (res.text or "").strip()
        return f"HTTP {res.status_code}" + (f" — {t[:280]}" if t else "")

    parts = [f"(header client id) {one(primary)}"]
    if secondary is not None:
        parts.append(f"(json client id) {one(secondary)}")
    return (
        "Tractive login failed after trying both API styles. "
        + " ".join(parts)
        + " Recheck TRACTIVE_EMAIL and TRACTIVE_PASSWORD in Railway (use the same password as in the Tractive app)."
    )


def tractive_get_token_and_user() -> tuple[str, str]:
    """Return (access_token, user_id) per Tractive Graph API conventions."""
    email = os.environ.get("TRACTIVE_EMAIL", "").strip()
    password = os.environ.get("TRACTIVE_PASSWORD", "").strip()
    if not email or not password:
        raise RuntimeError("Tractive is not configured (TRACTIVE_EMAIL / TRACTIVE_PASSWORD).")
    res = _tractive_auth_post(email, password, legacy_json_body=False)
    if res.status_code >= 400:
        res_alt = _tractive_auth_post(email, password, legacy_json_body=True)
        if res_alt.status_code < 400:
            res = res_alt
        else:
            raise RuntimeError(_tractive_auth_failed_message(res, res_alt))
    data = res.json()
    token = _dig(
        data,
        ["access_token", "accessToken", "token"],
    )
    if not token and isinstance(data, dict) and "user" in str(data).lower():
        for k, v in data.items():
            if "token" in k.lower() and isinstance(v, str) and len(v) > 20:
                token = v
    if not token and isinstance(data, str):
        token = data
    if not token:
        raise RuntimeError("Tractive returned a response we could not read. The service may have changed format.")
    uid = ""
    if isinstance(data, dict):
        uid = str(data.get("user_id") or data.get("userId") or "").strip()
    return str(token), uid


def _tractive_positions_api_versions() -> list[str]:
    """Prefer Graph API v4 (current aiotractive); fall back to v3."""
    raw = (os.environ.get("TRACTIVE_GRAPH_API_VERSION") or "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return ["4", "3"]


def tractive_fetch_positions(tracker_id: str, t_from: int, t_to: int) -> list[dict[str, Any]]:
    if not tracker_id:
        raise RuntimeError("Tractive tracker ID is missing. Set TRACTIVE_TRACKER_ID.")
    at, user_id = tractive_get_token_and_user()
    ph = _tractive_graph_headers(at, user_id)
    fmt = (
        os.environ.get("TRACTIVE_POSITIONS_FORMAT") or TRACTIVE_POSITIONS_FORMAT_DEFAULT
    ).strip()
    params = {
        "time_from": float(t_from),
        "time_to": float(t_to),
        "format": fmt,
    }
    last_payload: Any = None
    for ver in _tractive_positions_api_versions():
        url = f"https://graph.tractive.com/{ver}/tracker/{tracker_id}/positions"
        res = requests.get(url, params=params, headers=ph, timeout=60)
        if res.status_code == 404:
            raise RuntimeError(
                "Tractive returned 404 for this tracker. TRACTIVE_TRACKER_ID must be the "
                "device/tracker id from the Tractive app (often shown under the tracker or pet "
                "device settings), not only the pet's name."
            )
        if res.status_code >= 400:
            msg = f"Tractive could not return positions (HTTP {res.status_code}). Try a different time range."
            try:
                err = res.json()
                if isinstance(err, dict) and (err.get("message") or err.get("error")):
                    msg = f"Tractive: {err.get('message') or err.get('error')}"
            except Exception:
                pass
            raise RuntimeError(msg)
        last_payload = res.json()
        parsed = _parse_positions_response(last_payload)
        if parsed:
            return parsed
        _logger.warning(
            "Tractive positions API v%s returned no parseable points (keys=%s); trying fallback version if any.",
            ver,
            list(last_payload.keys()) if isinstance(last_payload, dict) else type(last_payload).__name__,
        )
    if isinstance(last_payload, dict):
        sample = {k: type(last_payload[k]).__name__ for k in list(last_payload.keys())[:12]}
        raise RuntimeError(
            "Tractive returned location data, but in a shape we could not read into GPS points. "
            f"API response keys: {sample}. Set TRACTIVE_GRAPH_API_VERSION=3 to force the older API, "
            "or widen the time range. Confirm TRACTIVE_TRACKER_ID is the tracker hardware id."
        )
    raise RuntimeError(
        "No GPS points from Tractive for that window. Widen the range, pick times when the pet "
        "was moving outside with a fix, and verify TRACTIVE_TRACKER_ID matches the tracker device id."
    )


def _looks_like_tractive_point(d: dict[str, Any]) -> bool:
    if _extract_lat_lon(d) is None:
        return False
    return _extract_time(d) is not None


def _scavenge_nested_points(obj: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Last-resort walk for nested json_segments / GeoJSON-like trees."""
    found: list[dict[str, Any]] = []
    if depth > 16:
        return found
    if isinstance(obj, dict):
        if _looks_like_tractive_point(obj):
            found.append(obj)
        for v in obj.values():
            found.extend(_scavenge_nested_points(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_scavenge_nested_points(item, depth + 1))
    return found


def _parse_positions_response(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        raw = None
        # json_segments often returns { "segments": [ { "points": [...] }, ... ] }
        segs = payload.get("segments")
        if isinstance(segs, list) and segs:
            merged: list[dict[str, Any]] = []
            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                sub: list[dict[str, Any]] | None = None
                for nk in ("points", "positions", "path", "data", "locations", "samples"):
                    pts = seg.get(nk)
                    if isinstance(pts, list):
                        sub = [p for p in pts if isinstance(p, dict)]
                        break
                if sub is not None:
                    merged.extend(sub)
                elif any(k in seg for k in ("lat", "latitude", "lng", "lon", "longitude")):
                    merged.append(seg)
            if merged:
                raw = merged
        if raw is None:
            for key in (
                "positions",
                "data",
                "pos",
                "points",
                "result",
                "items",
                "location_history",
                "path",
                "segments",
            ):
                v = payload.get(key)
                if v is not None and isinstance(v, (list, tuple)):
                    raw = v
                    break
        if raw is None and "payload" in payload:
            return _parse_positions_response(payload.get("payload"))
        if raw is None:
            raw = list(payload.values()) if payload else []
        if not isinstance(raw, (list, tuple)) and not raw:
            raw = []
    else:
        return []

    if not raw:
        return _scavenge_nested_points(payload)
    if isinstance(raw, tuple):
        raw = list(raw)
    if not raw:
        return _scavenge_nested_points(payload)
    if isinstance(raw[0], (list, tuple)) and len(raw[0]) >= 2 and not isinstance(
        raw[0][0] if len(raw) > 0 else None, dict
    ):
        return [{"lat": a[0], "lon": a[1] if len(a) > 1 else a[0], "t": a[2] if len(a) > 2 else 0} for a in raw]

    dicts = [p for p in raw if p is not None and isinstance(p, (dict,))]
    if dicts:
        return dicts
    return _scavenge_nested_points(payload)


def normalize_track_points(positions: list[dict[str, Any]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for p in positions:
        t = _extract_time(p)
        if t is None:
            continue
        ll = _extract_lat_lon(p)
        if ll is None:
            continue
        lat, lon = ll
        if lat is None or lon is None:
            continue
        entry: dict[str, float] = {
            "lat": float(lat),
            "lon": float(lon),
            "t": float(t),
        }
        a = _extract_alt(p)
        if a is not None:
            entry["alt"] = float(a)
        out.append(entry)
    out.sort(key=lambda x: x["t"])
    if not out:
        if positions:
            raise RuntimeError(
                "Tractive returned samples for that window, but none had usable GPS coordinates "
                "and timestamps. The API format may have changed."
            )
        raise RuntimeError(
            "No location points in that time range. Widen the window, pick times when the tracker "
            "had a GPS fix (outdoors), or check TRACTIVE_TRACKER_ID."
        )
    if len(out) < MIN_TRK_PTS:
        raise RuntimeError("Need at least two GPS points to build a run. Widen the time range.")
    if len({p["t"] for p in out}) < 2:
        raise RuntimeError("Not enough distinct GPS times to make a run track.")
    return out


def _extract_time(p: dict[str, Any]) -> float | None:
    if "position" in p and isinstance(p.get("position"), dict):
        inner_t = _extract_time(p["position"])
        if inner_t is not None:
            return inner_t
    for k in (
        "time",
        "timestamp",
        "ts",
        "t",
        "at",
        "datetime",
        "pos_time",
        "time_utc",
        "unlocked_at",
        "reported_at",
        "sample_time",
        "epoch",
        "unix",
        "unix_time",
    ):
        if k in p and p[k] is not None:
            v = p[k]
            if isinstance(v, (int, float)):
                return float(v) / 1000.0 if float(v) > 1e12 else float(v)
            if isinstance(v, str):
                s = v.strip()
                if s.isdigit():
                    f = float(s)
                    return f / 1000.0 if f > 1e12 else f
    return None


def _extract_lat_lon(p: dict[str, Any]) -> tuple[float, float] | None:
    # Tractive live reports and json_segments points often use latlong: [lat, lon]
    ll = p.get("latlong")
    if isinstance(ll, (list, tuple)) and len(ll) >= 2 and ll[0] is not None and ll[1] is not None:
        return float(ll[0]), float(ll[1])
    if "position" in p and isinstance(p.get("position"), dict):
        inner = _extract_lat_lon(p["position"])
        if inner is not None:
            return inner
    if "lat" in p and "lon" in p and p["lat"] is not None and p["lon"] is not None:
        return float(p["lat"]), float(p["lon"])
    if "lat" in p and "lng" in p and p["lat"] is not None and p["lng"] is not None:
        return float(p["lat"]), float(p["lng"])
    if "lat" in p and "long" in p:
        return float(p["lat"]), float(p["long"])
    if "latitude" in p and "longitude" in p:
        return float(p["latitude"]), float(p["longitude"])
    if "y" in p and "x" in p and isinstance(p.get("x"), (int, float, str)):
        return float(p.get("y")), float(p.get("x"))
    if "pos" in p and isinstance(p["pos"], (list, tuple)) and len(p["pos"]) >= 2:
        a = p["pos"]
        return float(a[0]), float(a[1])
    if "latlng" in p and isinstance(p["latlng"], (list, tuple)) and len(p["latlng"]) >= 2:
        a = p["latlng"]
        return float(a[0]), float(a[1])
    if "ll" in p and isinstance(p["ll"], (list, tuple)) and len(p["ll"]) >= 2:
        a = p["ll"]
        return float(a[0]), float(a[1])
    return None


def _extract_alt(p: dict[str, Any]) -> float | None:
    for k in (
        "alt",
        "altitude",
        "ele",
        "elevation",
        "height",
    ):
        if k in p and p[k] is not None:
            try:
                return float(p[k])
            except (TypeError, ValueError):
                pass
    if "z" in p and p.get("z") is not None:
        try:
            return float(p["z"])
        except (TypeError, ValueError):
            return None
    return None


def _dig(d: Any, names: list[str]) -> Any:
    for n in names:
        if isinstance(d, dict) and n in d:
            return d[n]
    return None


# ---------------------------------------------------------------------------
# GPX 1.1
# ---------------------------------------------------------------------------


def build_gpx(points: list[dict[str, float]], name: str = "Run") -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            "<gpx version='1.1' creator='Runkiki' "
            "xmlns='http://www.topografix.com/GPX/1/1' "
            "xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance' "
            "xsi:schemaLocation='http://www.topografix.com/GPX/1/1 "
            "http://www.topografix.com/GPX/1/1/gpx.xsd'>"
        ),
        f"<metadata><name>{xml_esc.escape(name)}</name><time>{_iso_utc_from_epoch(points[0]['t'])}</time></metadata>",
        f"<trk><name>{xml_esc.escape(name)}</name><trkseg>",
    ]
    for pt in points:
        lat = float(pt["lat"])
        lon = float(pt["lon"])
        t = _iso_utc_from_epoch(pt["t"])
        s = f'<trkpt lat="{lat}" lon="{lon}"><time>{t}</time>'
        if "alt" in pt:
            s += f'<ele>{float(pt["alt"])}</ele>'
        s += "</trkpt>"
        lines.append(s)
    lines.append("</trkseg></trk></gpx>")
    return "\n".join(lines)


def _iso_utc_from_epoch(ts: float) -> str:
    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def track_distance_m(points: list[dict[str, float]]) -> float:
    d = 0.0
    for i in range(1, len(points)):
        d += _haversine_m(
            float(points[i - 1]["lat"]),
            float(points[i - 1]["lon"]),
            float(points[i]["lat"]),
            float(points[i]["lon"]),
        )
    return d


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


# ---------------------------------------------------------------------------
# Strava
# ---------------------------------------------------------------------------


def strava_state_path() -> str:
    return os.environ.get("STRAVA_TOKENS_PATH", STRAVA_TOKENS_PATH)


def strava_read_state() -> dict[str, Any]:
    path = strava_state_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def strava_write_state(state: dict[str, Any]) -> None:
    path = strava_state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=0)
    if os.name != "nt" and str(path).startswith("/tmp/"):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _env_strip(key: str) -> str | None:
    v = os.environ.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _parse_strava_expires_at(raw: str | None) -> int:
    """Unix seconds for token expiry: plain number or ISO-8601 (e.g. 2026-04-30T17:17:08Z)."""
    if raw is None:
        return 0
    s = str(raw).strip()
    if not s:
        return 0
    try:
        v = float(s)
        if v > 1e12:
            v /= 1000.0
        return int(v)
    except ValueError:
        pass
    try:
        iso = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except ValueError:
        return 0


def _normalize_strava_token(raw: str | None) -> str | None:
    """Strip whitespace and accidental 'Bearer ' prefix from pasted Railway tokens."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s if s else None


def strava_merged_creds() -> dict[str, Any]:
    s = strava_read_state()
    ex = s.get("expires_at")
    if ex is not None and isinstance(ex, (int, float, str)) and str(ex).replace(".", "").isdigit():
        ex_v = int(float(str(ex)))
    else:
        ex_v = 0
    if ex_v and ex_v < 1e10:
        ex_v = ex_v
    if ex_v and ex_v < 1e10:
        pass
    ex_env_v = _parse_strava_expires_at(os.environ.get("STRAVA_TOKEN_EXPIRES_AT"))
    if ex_env_v:
        ex_v = ex_env_v
    return {
        "client_id": (s.get("client_id") or _env_strip("STRAVA_CLIENT_ID")) or None,
        "client_secret": (s.get("client_secret") or _env_strip("STRAVA_CLIENT_SECRET")) or None,
        "access_token": _normalize_strava_token(
            str(s.get("access_token")) if s.get("access_token") else _env_strip("STRAVA_ACCESS_TOKEN")
        ),
        "refresh_token": _normalize_strava_token(
            str(s.get("refresh_token")) if s.get("refresh_token") else _env_strip("STRAVA_REFRESH_TOKEN")
        ),
        "expires_at": ex_v,
    }


def get_valid_strava_token(force_refresh: bool = False) -> str:
    c = strava_merged_creds()
    now = time.time()
    try:
        exp_at_f = float(c.get("expires_at") or 0)
    except (TypeError, ValueError):
        exp_at_f = 0.0
    # Stale only when we have a real expiry timestamp and it has passed (with 2 min skew).
    token_stale = bool(exp_at_f > 1e9 and now >= exp_at_f - 120)
    # No expires_at in env/file → do not trust a pasted STRAVA_ACCESS_TOKEN; refresh if possible.
    expiry_unknown = exp_at_f <= 1e9
    can_refresh = bool(
        c.get("refresh_token") and c.get("client_id") and c.get("client_secret")
    )

    if not force_refresh and c.get("access_token") and not token_stale:
        if expiry_unknown and can_refresh:
            pass  # exchange refresh for a known-good access token
        else:
            return str(c["access_token"])

    # Expired or missing access token: use refresh (also covers Railway with only refresh + client id/secret).
    if not c.get("refresh_token"):
        raise RuntimeError(
            "Strava is not connected. Open /auth after setting STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, "
            "and STRAVA_REDIRECT_URI. Or set STRAVA_REFRESH_TOKEN (and STRAVA_CLIENT_SECRET / STRAVA_CLIENT_ID) "
            "in Railway — STRAVA_ACCESS_TOKEN alone is optional if you have a refresh token."
        )
    if not c.get("client_id") or not c.get("client_secret"):
        raise RuntimeError(
            "STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET are required to refresh the Strava token."
        )
    r = requests.post(
        f"{STRAVA_OAUTH}/token",
        data={
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "refresh_token": c["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if r.status_code >= 400:
        err = f"Strava would not refresh the access token (HTTP {r.status_code}). Visit /auth to link Strava again."
        try:
            o = r.json()
            if isinstance(o, dict) and o.get("message"):
                err = f"Strava: {o.get('message')}"
        except Exception:
            pass
        raise RuntimeError(err)
    data = r.json()
    at = data.get("access_token")
    rt = data.get("refresh_token", c.get("refresh_token"))
    ex_in = int(data.get("expires_in", 0))
    exp = int(time.time() + ex_in) if ex_in else int(time.time() + 21600)
    merged = strava_merged_creds()
    to_save: dict[str, Any] = {
        "access_token": at,
        "refresh_token": rt,
        "expires_at": exp,
    }
    for k, v in merged.items():
        if v and k not in to_save:
            to_save[k] = v
    w = {**strava_read_state(), **{k: v for k, v in to_save.items() if v is not None}}
    w["client_id"] = c.get("client_id")
    w["client_secret"] = c.get("client_secret")
    w["access_token"] = at
    w["refresh_token"] = rt
    w["expires_at"] = exp
    strava_write_state(w)
    if not at:
        raise RuntimeError("Strava refresh response did not include an access token.")
    return str(at)


def strava_authorized_get(path: str, **kwargs) -> requests.Response:
    t = get_valid_strava_token()
    h = {"Authorization": f"Bearer {t}"}
    if "headers" in kwargs:
        h.update(kwargs.pop("headers"))
    return requests.get(f"{STRAVA_API}{path}", headers=h, **kwargs)


def strava_authorized_post(path: str, **kwargs) -> requests.Response:
    t = get_valid_strava_token()
    h = {"Authorization": f"Bearer {t}"}
    if "headers" in kwargs:
        h.update(kwargs.pop("headers"))
    return requests.post(f"{STRAVA_API}{path}", headers=h, **kwargs)


def strava_authorized_put(path: str, **kwargs) -> requests.Response:
    t = get_valid_strava_token()
    h = {
        "Authorization": f"Bearer {t}",
        "Content-Type": "application/json",
    }
    if "headers" in kwargs:
        h.update(kwargs.pop("headers"))
    return requests.put(f"{STRAVA_API}{path}", headers=h, **kwargs)


def _strava_response_suggests_bad_token(resp: requests.Response) -> bool:
    if resp.status_code in (401, 403):
        return True
    try:
        o = resp.json()
        msg = str(o.get("message") or o.get("error") or "").lower()
        if "authorization" in msg or "invalid access token" in msg or "token expired" in msg:
            return True
    except Exception:
        pass
    return False


def strava_upload_gpx(
    gpx_bytes: bytes,
    *,
    name: str,
    description: str,
    commute: bool,
) -> int:
    data: dict[str, str] = {
        "data_type": "gpx",
        "name": name or "Run",
    }
    if description:
        data["description"] = description
    data["commute"] = "1" if commute else "0"
    data["activity_type"] = "run"

    def _do_upload(token: str) -> requests.Response:
        files = {
            "file": ("run_logger.gpx", BytesIO(gpx_bytes), "application/gpx+xml"),
        }
        return requests.post(
            f"{STRAVA_API}/uploads",
            data=data,
            files=files,
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )

    t = get_valid_strava_token()
    r = _do_upload(t)
    if r.status_code >= 400 and _strava_response_suggests_bad_token(r):
        t2 = get_valid_strava_token(force_refresh=True)
        r = _do_upload(t2)

    if r.status_code >= 400:
        m = f"Strava would not accept the file (HTTP {r.status_code})."
        try:
            o = r.json()
            if isinstance(o, dict) and o.get("message"):
                m = f"Strava: {o.get('message')}"
        except Exception:
            m = f"Strava upload failed (HTTP {r.status_code})."
        raise RuntimeError(m)
    u = r.json()
    if "error" in u and u.get("error") and u.get("status", "").find("error") != -1:
        raise RuntimeError(
            f"Strava: {u.get('error', 'Unknown error when uploading the route file. Try again soon.')}"
        )
    uid = u.get("id")
    if not uid:
        raise RuntimeError("Strava did not return an upload id.")
    return int(uid)


def strava_poll_upload(uid: int) -> int:
    deadline = time.time() + UPLOAD_POLL_MAX_SEC
    while time.time() < deadline:
        t = get_valid_strava_token()
        r = requests.get(
            f"{STRAVA_API}/uploads/{uid}",
            headers={"Authorization": f"Bearer {t}"},
            timeout=30,
        )
        r.raise_for_status()
        s = r.json()
        if s.get("activity_id"):
            return int(s["activity_id"])
        e = s.get("error")
        if e and str(e).strip() and str(e).lower() not in ("null", "none", ""):
            err_clean = re.sub(r"<[^>]+>", " ", str(e))
            raise RuntimeError(
                f"Strava had trouble with that file: {err_clean.strip() or 'Check the run times and file format.'}"
            )
        time.sleep(UPLOAD_POLL_INTERVAL)
    return 0


def strava_get_activity(aid: int) -> dict[str, Any]:
    t = get_valid_strava_token()
    r = requests.get(
        f"{STRAVA_API}/activities/{aid}",
        params={"include_all_efforts": "false"},
        headers={"Authorization": f"Bearer {t}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def strava_update_activity(
    aid: int,
    *,
    name: str,
    description: str,
    commute: bool,
    perceived_exertion: int | None,
) -> tuple[dict[str, Any], str | None]:
    """Update activity. Returns (activity json, note if RPE was stored in description instead)."""
    body: dict[str, Any] = {
        "name": name,
        "commute": bool(commute),
        "description": description or "",
    }
    r = strava_authorized_put(f"/activities/{aid}", json=body)
    if r.status_code not in (200, 201):
        m = f"Strava would not update the activity (HTTP {r.status_code})."
        try:
            o = r.json()
            if isinstance(o, dict) and o.get("message"):
                m = f"Strava: {o.get('message')}"
        except Exception:
            pass
        raise RuntimeError(m)
    out: dict[str, Any] = r.json() if r.content else body
    pe: int | None
    if perceived_exertion and 1 <= int(perceived_exertion) <= 10:
        pe = int(perceived_exertion)
    else:
        return out, None
    r2 = strava_authorized_put(
        f"/activities/{aid}",
        json={**body, "perceived_exertion": pe},
    )
    if r2.status_code in (200, 201) and r2.content:
        j = r2.json()
        if j.get("perceived_exertion") is not None:
            return j, None
    n = "Strava may not set perceived exertion from the app connection, so it was added to the notes."
    line = f"Felt: {pe}/10"
    base = (description or "").rstrip()
    if line in (description or ""):
        desc2 = description or ""
    else:
        desc2 = f"{base}\n\n{line}" if base else line
    r3 = strava_authorized_put(
        f"/activities/{aid}",
        json={**body, "description": desc2},
    )
    r3.raise_for_status()
    return r3.json() if r3.content else out, n


def strava_post_activity_photos(activity_id: int, files: list) -> str | None:
    """
    Tries the native photo upload. Returns a human string if it did not work.
    """
    if not files:
        return None
    t = get_valid_strava_token()
    warnings = []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            f.stream.seek(0)
        except (OSError, ValueError, AttributeError):
            pass
        files_up = {
            "file": (
                f.filename,
                f.stream,
                f.mimetype or "application/octet-stream",
            )
        }
        r = requests.post(
            f"{STRAVA_API}/activities/{activity_id}/photos",
            files=files_up,
            headers={"Authorization": f"Bearer {t}"},
            timeout=120,
        )
        if r.status_code in (200, 201, 202, 204):
            continue
        w = f"Strava would not add one photo (HTTP {r.status_code})."
        try:
            o = r.json()
            if isinstance(o, dict) and o.get("message"):
                w = str(o.get("message"))
        except Exception:
            pass
        warnings.append(w)
    if warnings:
        return (
            "The run was created, but Strava may not have attached every photo. "
            + (warnings[0] if len(warnings) == 1 else (warnings[0] + " Add photos on Strava if you like."))
        )
    return None


def _allowed_image(f) -> bool:
    ext = os.path.splitext((f.filename or "").lower())[1]
    if ext not in (".jpg", ".jpeg", ".png"):
        return False
    try:
        f.stream.seek(0, 2)
        sz = f.stream.tell()
        f.stream.seek(0)
        if sz and sz > MAX_PHOTO_BYTES:
            return False
    except (OSError, ValueError, AttributeError, TypeError):
        f.stream.seek(0)
    return True


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_PHOTO_BYTES * 6
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "dev-unsafe-change-in-production"

    @app.before_request
    def _gate():
        p = (request.path or "/").rstrip() or "/"
        if p in (
            "/login",
            "/auth",
            "/auth/callback",
            "/favicon.ico",
            "/api/version",
            "/version",
        ) or p.startswith("/static/"):
            return None
        if session.get("authed") is not True:
            if p.startswith("/api/"):
                return jsonify(error="Not signed in. Open the site and sign in at /login first."), 401
            return redirect(url_for("login"))
        return None

    @app.get("/favicon.ico")
    def _fav():
        return ("", 204)

    @app.get("/api/version")
    @app.get("/version")
    def api_version():
        """Public build fingerprint — open in a browser to confirm Railway deployed this revision."""
        return jsonify(
            app="runkiki",
            build=os.environ.get("RUNKIKI_BUILD", RUNKIKI_BUILD_ID),
            tractive_auth="header-json-fallback-legacy-json",
        )

    @app.get("/")
    def index():
        if session.get("authed") is not True:
            return redirect(url_for("login"))
        return render_template("index.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        err = None
        if request.method == "POST":
            pw = request.form.get("password", "")
            if pw and pw == (os.environ.get("ACCESS_PASSWORD") or ""):
                session["authed"] = True
                return redirect(url_for("index"))
            err = "That password is not right. Try again."
        return _login_page(err)

    @app.get("/auth")
    def strava_start():
        cid = os.environ.get("STRAVA_CLIENT_ID", "")
        ruri = (os.environ.get("STRAVA_REDIRECT_URI") or "").strip()
        if not cid or not ruri:
            return (
                "Set STRAVA_CLIENT_ID and STRAVA_REDIRECT_URI in the environment, then open this page again.",
                500,
            )
        scope = "read,activity:read,activity:write"
        u = (
            f"{STRAVA_OAUTH}/authorize?client_id={cid}"
            f"&redirect_uri={requests.utils.quote(ruri, safe='')}"
            f"&response_type=code&scope={requests.utils.quote(scope)}&approval_prompt=auto"
        )
        return redirect(u)

    @app.get("/auth/callback")
    def strava_callback():
        code = request.args.get("code", "")
        errp = request.args.get("error", "")
        if errp:
            return f"Strava said no: {errp}", 400
        if not code:
            return "Missing code from Strava. Go back to /auth and try again.", 400
        cid = os.environ.get("STRAVA_CLIENT_ID", "")
        csec = os.environ.get("STRAVA_CLIENT_SECRET", "")
        ruri = (os.environ.get("STRAVA_REDIRECT_URI") or "").strip()
        r = requests.post(
            f"{STRAVA_OAUTH}/token",
            data={
                "client_id": cid,
                "client_secret": csec,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": ruri,
            },
            timeout=30,
        )
        if r.status_code >= 400:
            return f"Strava would not give tokens: {r.text[:500]}", 400
        data = r.json()
        at = data.get("access_token")
        rt = data.get("refresh_token")
        ex = int(data.get("expires_in", 0) or 0) + int(time.time())
        st = {**strava_read_state(), "access_token": at, "refresh_token": rt, "expires_at": ex}
        st["client_id"] = os.environ.get("STRAVA_CLIENT_ID", "")
        st["client_secret"] = os.environ.get("STRAVA_CLIENT_SECRET", "")
        if at and rt:
            strava_write_state(st)
        return _ok_html("Strava is connected. You can return to Runkiki and log a run. Copy new tokens from /tmp to Railway if needed.")

    @app.post("/api/log-run")
    def log_run():
        if session.get("authed") is not True:
            return jsonify(error="Not signed in."), 401
        try:
            t_from, t_to = _parse_time_window()
        except ValueError as e:
            current_app.logger.warning("log-run bad time window: %s", e)
            return jsonify(error=str(e)), 400
        name = (request.form.get("activity_name") or "").strip() or "Run"
        description = (request.form.get("description") or "").strip()
        travel = request.form.get("commute", "") in ("1", "true", "on", "yes")
        ex_raw = (request.form.get("perceived_exertion") or "").strip()
        ex = None
        if ex_raw.isdigit() and 1 <= int(ex_raw) <= 10:
            ex = int(ex_raw)
        photos = request.files.getlist("photos")
        try:
            t_id = os.environ.get("TRACTIVE_TRACKER_ID", "").strip()
            rawp = tractive_fetch_positions(t_id, int(t_from), int(t_to))
            npts = normalize_track_points(rawp)
            gpx = build_gpx(npts, name=name)
            gpx_m = track_distance_m(npts)
        except Exception as e2:
            msg = str(e2) or "Could not load your tracker for that time range."
            current_app.logger.warning("log-run Tractive/GPX failed: %s", msg)
            return jsonify(error=msg), 400

        try:
            up_id = strava_upload_gpx(
                gpx.encode("utf-8"),
                name=name,
                description=description,
                commute=travel,
            )
        except Exception as e:
            msg = str(e)
            current_app.logger.warning("log-run Strava upload failed: %s", msg)
            return jsonify(error=msg), 400
        aid = strava_poll_upload(int(up_id))
        if not aid:
            return jsonify(
                error="Strava is still processing the file, but it took too long. Check Strava in a minute; the run may show up as processing."
            ), 502
        photo_warn: str | None = None
        rpe_note: str | None = None
        update_error: str | None = None
        try:
            _, rpe_note = strava_update_activity(
                aid,
                name=name,
                description=description,
                commute=travel,
                perceived_exertion=ex,
            )
        except Exception as e3:
            update_error = (
                str(e3)
                or "The run is on Strava, but a few details could not be set from this app. You can fix them in Strava."
            )
        try:
            d = strava_get_activity(aid)
        except Exception:
            d = {}
        dist = float(d.get("distance", 0) or gpx_m)
        if dist <= 0:
            dist = gpx_m
        moving = d.get("moving_time")
        if not moving:
            moving = int(max(0, t_to - t_from))
        to_upload: list = []
        for p in photos:
            if p and p.filename and _allowed_image(p):
                p.stream.seek(0)
                to_upload.append(p)
        w = strava_post_activity_photos(aid, to_upload)
        if w:
            photo_warn = w
        out: dict[str, Any] = {
            "ok": True,
            "activity_id": aid,
            "distance_m": dist,
            "duration_s": int(moving) if moving else int(t_to - t_from),
            "strava_url": f"https://www.strava.com/activities/{aid}",
        }
        if rpe_note:
            out["perceived_exertion_note"] = rpe_note
        if update_error:
            out["update_error"] = update_error
        if photo_warn:
            out["photo_note"] = photo_warn
        return jsonify(out)

    return app


def _parse_time_window() -> tuple[int, int]:
    """Prefer browser-computed Unix epochs (local timezone); fallback to date/time strings."""
    se_raw = (request.form.get("start_epoch") or "").strip()
    ee_raw = (request.form.get("end_epoch") or "").strip()
    if se_raw.isdigit() and ee_raw.isdigit():
        t_from = int(se_raw)
        t_to = int(ee_raw)
        if t_to <= t_from:
            raise ValueError("End time has to be after the start time.")
        return t_from, t_to

    st = (request.form.get("start") or "").strip()
    en = (request.form.get("end") or "").strip()
    if not st or not en:
        parts = {
            "sd": request.form.get("start_date"),
            "st": request.form.get("start_time"),
            "ed": request.form.get("end_date"),
            "et": request.form.get("end_time"),
        }
        for k, v in parts.items():
            if v is None and k not in ():
                pass
        s = _combine_date_time(
            (request.form.get("start_date") or "").strip(),
            (request.form.get("start_time") or "").strip(),
        )
        e = _combine_date_time(
            (request.form.get("end_date") or "").strip(),
            (request.form.get("end_time") or "").strip(),
        )
        st = s or st
        en = e or en
    if not st or not en:
        raise ValueError("Set both start and end for your run (date and time).")
    t_from = int(_to_epoch(st))
    t_to = int(_to_epoch(en))
    if t_to <= t_from:
        raise ValueError("End time has to be after the start time.")
    return t_from, t_to


def _combine_date_time(d: str, t: str) -> str:
    if d and t:
        return f"{d}T{t}"
    if d and not t:
        return f"{d}T00:00:00"
    if not d and t:
        from datetime import date

        return f"{date.today().isoformat()}T{t}"
    return ""


def _to_epoch(s: str) -> float:
    s = s.strip()
    s = s.replace("Z", "+00:00")
    if re.match(r"^[-+]?\d+$", s) or re.match(r"^[-+]?\d+\.?\d+$", s):
        v = float(s)
        if v > 1e12:
            return v / 1000.0
        return v
    try:
        d = datetime.fromisoformat(s)
    except ValueError as e:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                d = datetime.strptime(s, fmt)  # noqa: DTZ007
                d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
                return d.timestamp()
            except ValueError:
                pass
        raise ValueError("Could not read the date and time you entered. Use a clear time range.") from e
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _ok_html(msg: str) -> str:
    return render_template_string(
        """<!doctype html><html><head><meta charset="utf-8">
<title>Runkiki</title><body style="font:16px/1.5 system-ui;max-width:32rem;padding:2rem">
<p>{{ message }}</p>
<p><a href="/">Back to Runkiki</a></p></body></html>""",
        message=msg,
    )


def _login_page(err: str | None) -> str:
    return render_template_string(
        """<!doctype html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in — Runkiki</title>
<style>
body { font: 16px/1.5 system-ui, -apple-system, sans-serif; background: #fff; color: #111; margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.card { max-width: 24rem; width: 100%; margin: 1.5rem; padding: 2rem; box-shadow: 0 2px 24px rgba(0,0,0,.08); border-radius: 16px; }
label { display: block; font-size: 0.875rem; color: #444; margin-bottom: 0.35rem; }
input { width: 100%; box-sizing: border-box; margin-bottom: 1rem; padding: 0.65rem 0.75rem; border: 1px solid #e0e0e0; border-radius: 10px; }
button { width: 100%; background: #007aff; color: #fff; border: none; padding: 0.75rem; border-radius: 10px; font-weight: 600; cursor: pointer; }
h1 { font-size: 1.25rem; margin: 0 0 1rem; }
</style></head><body>
<div class="card"><h1>Runkiki</h1>
<p>Enter the app password to continue.</p>
<form method="post">{{ e|safe }}
  <label for="password">Password</label>
  <input id="password" name="password" type="password" required autocomplete="current-password">
  <button type="submit">Sign in</button>
</form>
</div></body></html>""",
        e=(login_err_html(err) if err else ""),
    )


def login_err_html(err: str) -> str:
    return f'<p class="form-error" style="color:#b00" role="alert">{xml_esc.escape(err)}</p>'


app = create_app()

if __name__ == "__main__":
    _port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=_port, debug=False)
