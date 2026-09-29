"""Geography helpers: distance/time matrices, OSRM access and Plus code decoding.

Two ways to build the travel matrix:

* ``haversine`` – offline, no API key. Straight-line distance multiplied by a
  circuity factor (roads are never straight) and converted to minutes with an
  average speed. Good enough for planning in a dense city once the two factors
  are calibrated against a few real trips.
* ``osrm`` – real road distances/durations from an OSRM server (the public demo
  server by default). Durations have no traffic, so a traffic multiplier is
  applied. Falls back to ``haversine`` if the server can't be reached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import requests

EARTH_RADIUS_KM = 6371.0088
DEFAULT_OSRM_URL = "https://router.project-osrm.org"
OSRM_MAX_COORDS = 100  # the public demo server rejects bigger table requests


# --------------------------------------------------------------------------- #
# Distance / time matrices
# --------------------------------------------------------------------------- #
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def haversine_matrix_km(coords: list[tuple[float, float]]) -> np.ndarray:
    """Vectorised pairwise haversine distances (km) for ``[(lat, lon), ...]``."""
    arr = np.radians(np.asarray(coords, dtype=float))
    lat = arr[:, 0][:, None]
    lon = arr[:, 1][:, None]
    dlat = lat.T - lat
    dlon = lon.T - lon
    a = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(lat.T) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


@dataclass
class TravelMatrix:
    distance_m: np.ndarray  # int metres, shape (n, n)
    time_min: np.ndarray  # int minutes, shape (n, n)
    source: str  # "haversine" or "osrm"
    note: str = ""


def build_haversine_matrix(
    coords: list[tuple[float, float]],
    circuity: float = 1.35,
    speed_kmh: float = 22.0,
) -> TravelMatrix:
    km = haversine_matrix_km(coords) * circuity
    minutes = km / max(speed_kmh, 1e-6) * 60.0
    return TravelMatrix(
        distance_m=np.rint(km * 1000).astype(np.int64),
        time_min=np.ceil(minutes).astype(np.int64),
        source="haversine",
    )


def _coord_str(coords: list[tuple[float, float]]) -> str:
    # OSRM wants lon,lat
    return ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)


def fetch_osrm_table(
    coords: list[tuple[float, float]],
    base_url: str = DEFAULT_OSRM_URL,
    timeout: float = 30.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (distance_m, duration_s) matrices from an OSRM ``/table`` call."""
    if len(coords) > OSRM_MAX_COORDS:
        raise ValueError(
            f"OSRM table limited to {OSRM_MAX_COORDS} points here "
            f"(got {len(coords)}). Use your own OSRM server or haversine mode."
        )
    url = f"{base_url.rstrip('/')}/table/v1/driving/{_coord_str(coords)}"
    resp = requests.get(url, params={"annotations": "distance,duration"}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM error: {data.get('code')} {data.get('message', '')}")
    dist = np.array(data["distances"], dtype=float)
    dur = np.array(data["durations"], dtype=float)
    if np.isnan(dist).any() or np.isnan(dur).any():
        raise RuntimeError("OSRM returned unreachable pairs (null values).")
    return dist, dur


def build_osrm_matrix(
    coords: list[tuple[float, float]],
    base_url: str = DEFAULT_OSRM_URL,
    traffic_factor: float = 1.5,
    fallback_circuity: float = 1.35,
    fallback_speed_kmh: float = 22.0,
) -> TravelMatrix:
    """OSRM matrix with graceful fallback to the haversine estimate."""
    try:
        dist, dur = fetch_osrm_table(coords, base_url)
    except Exception as exc:  # network down, rate-limited, too many points...
        tm = build_haversine_matrix(coords, fallback_circuity, fallback_speed_kmh)
        tm.note = f"OSRM unavailable ({exc}); used haversine estimate instead."
        return tm
    return TravelMatrix(
        distance_m=np.rint(dist).astype(np.int64),
        time_min=np.ceil(dur / 60.0 * traffic_factor).astype(np.int64),
        source="osrm",
    )


def fetch_osrm_route_geometry(
    coords: list[tuple[float, float]],
    base_url: str = DEFAULT_OSRM_URL,
    timeout: float = 30.0,
) -> list[tuple[float, float]] | None:
    """Road-following polyline ``[(lat, lon), ...]`` for an ordered stop list.

    Returns ``None`` on any failure so callers can draw straight lines instead.
    """
    if len(coords) < 2 or len(coords) > OSRM_MAX_COORDS:
        return None
    url = f"{base_url.rstrip('/')}/route/v1/driving/{_coord_str(coords)}"
    try:
        resp = requests.get(
            url, params={"overview": "full", "geometries": "geojson"}, timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            return None
        line = data["routes"][0]["geometry"]["coordinates"]
        return [(lat, lon) for lon, lat in line]
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Plus codes (Open Location Code) – decoding only, no external dependency
# --------------------------------------------------------------------------- #
_OLC_ALPHABET = "23456789CFGHJMPQRVWX"
_OLC_SEP = "+"
_OLC_SEP_POS = 8
_OLC_PAD = "0"
_OLC_PAIR_RES = [20.0, 1.0, 0.05, 0.0025, 0.000125]
_OLC_GRID_ROWS = 5
_OLC_GRID_COLS = 4


def _clean_plus_code(code: str) -> str:
    # "QG7R+XX Bangkok" -> "QG7R+XX"
    return str(code).strip().split()[0].upper() if str(code).strip() else ""


def is_valid_plus_code(code: str) -> bool:
    code = _clean_plus_code(code)
    if code.count(_OLC_SEP) != 1:
        return False
    sep = code.index(_OLC_SEP)
    if sep > _OLC_SEP_POS or sep % 2 == 1:
        return False
    body = code.replace(_OLC_SEP, "")
    if _OLC_PAD in body:
        stripped = body.rstrip(_OLC_PAD)
        if _OLC_PAD in stripped:
            return False
        body = stripped
    if len(code) - sep - 1 == 1:  # a single char after "+" is not allowed
        return False
    return all(ch in _OLC_ALPHABET for ch in body)


def is_full_plus_code(code: str) -> bool:
    code = _clean_plus_code(code)
    return is_valid_plus_code(code) and code.index(_OLC_SEP) == _OLC_SEP_POS


def _decode_full(code: str) -> tuple[float, float, float, float]:
    """Return (lat_lo, lon_lo, lat_res, lon_res) for a full code."""
    digits = code.replace(_OLC_SEP, "").rstrip(_OLC_PAD)
    lat, lon = -90.0, -180.0
    lat_res = lon_res = 0.0
    pair_digits = digits[:10]
    for i in range(0, len(pair_digits), 2):
        res = _OLC_PAIR_RES[i // 2]
        lat += _OLC_ALPHABET.index(pair_digits[i]) * res
        if i + 1 < len(pair_digits):
            lon += _OLC_ALPHABET.index(pair_digits[i + 1]) * res
        lat_res = lon_res = res
    lat_res_g, lon_res_g = lat_res, lon_res
    for ch in digits[10:15]:
        lat_res_g /= _OLC_GRID_ROWS
        lon_res_g /= _OLC_GRID_COLS
        idx = _OLC_ALPHABET.index(ch)
        lat += (idx // _OLC_GRID_COLS) * lat_res_g
        lon += (idx % _OLC_GRID_COLS) * lon_res_g
    if len(digits) > 10:
        lat_res, lon_res = lat_res_g, lon_res_g
    return lat, lon, lat_res, lon_res


def _encode_pairs(lat: float, lon: float, length: int) -> str:
    """Encode the first ``length`` (even, <= 10) digits of a code."""
    lat = min(max(lat, -90.0), 90.0 - 1e-9) + 90.0
    lon = ((lon + 180.0) % 360.0)
    out = []
    for i in range(length // 2):
        res = _OLC_PAIR_RES[i]
        d_lat = int(math.floor(lat / res + 1e-12))
        d_lon = int(math.floor(lon / res + 1e-12))
        d_lat = min(d_lat, 19)
        d_lon = min(d_lon, 19)
        out.append(_OLC_ALPHABET[d_lat] + _OLC_ALPHABET[d_lon])
        lat -= d_lat * res
        lon -= d_lon * res
    return "".join(out)


def decode_plus_code(
    code: str, ref_lat: float | None = None, ref_lon: float | None = None
) -> tuple[float, float]:
    """Decode a full or short Plus code to its centre ``(lat, lon)``.

    Short codes such as ``"QG7R+XX"`` need a reference point (the depot is a
    good one); the nearest matching area to the reference is returned.
    """
    code = _clean_plus_code(code)
    if not is_valid_plus_code(code):
        raise ValueError(f"Invalid Plus code: {code!r}")

    if not is_full_plus_code(code):
        if ref_lat is None or ref_lon is None:
            raise ValueError(f"Short Plus code {code!r} needs a reference location")
        pad_len = _OLC_SEP_POS - code.index(_OLC_SEP)
        resolution = 20.0 ** (2 - pad_len / 2)
        half = resolution / 2.0
        full = _encode_pairs(ref_lat, ref_lon, pad_len) + code
        lat_lo, lon_lo, lat_res, lon_res = _decode_full(full)
        lat_c, lon_c = lat_lo + lat_res / 2, lon_lo + lon_res / 2
        if ref_lat + half < lat_c and lat_c - resolution >= -90:
            lat_c -= resolution
        elif ref_lat - half > lat_c and lat_c + resolution <= 90:
            lat_c += resolution
        if ref_lon + half < lon_c:
            lon_c -= resolution
        elif ref_lon - half > lon_c:
            lon_c += resolution
        return lat_c, lon_c

    lat_lo, lon_lo, lat_res, lon_res = _decode_full(code)
    return lat_lo + lat_res / 2, lon_lo + lon_res / 2
