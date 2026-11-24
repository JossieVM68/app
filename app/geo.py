"""Distancia a casa y pago por hora."""
from __future__ import annotations

import math
import re
import time

import httpx

from . import config

_cache: dict[str, tuple[float, float] | None] = {}
_last_osm = 0.0


def zip_of(s: str) -> str:
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", s or "")
    return m.group(1) if m else ""


def is_specific(addr: str) -> bool:
    """Solo calculamos distancia si hay calle con número o código postal (no solo 'Chicago, IL')."""
    a = addr or ""
    return bool(re.search(r"\b\d{2,6}\s+\w+", a) or zip_of(a))


def geocode(addr: str) -> tuple[float, float] | None:
    global _last_osm
    addr = (addr or "").strip()
    if not addr:
        return None
    if addr in _cache:
        return _cache[addr]
    res = None
    try:
        with httpx.Client(timeout=15, headers={"User-Agent": config.USER_AGENT}) as c:
            r = c.get("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
                      params={"address": addr, "benchmark": "Public_AR_Current", "format": "json"})
            if r.status_code == 200:
                m = r.json().get("result", {}).get("addressMatches", [])
                if m:
                    res = (m[0]["coordinates"]["y"], m[0]["coordinates"]["x"])
            if res is None:
                wait = 1.1 - (time.time() - _last_osm)
                if wait > 0:
                    time.sleep(wait)
                _last_osm = time.time()
                r = c.get("https://nominatim.openstreetmap.org/search",
                          params={"q": addr, "format": "json", "limit": 1, "countrycodes": "us"})
                if r.status_code == 200 and r.json():
                    res = (float(r.json()[0]["lat"]), float(r.json()[0]["lon"]))
    except Exception:
        return None
    _cache[addr] = res
    return res


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|K)?")


def parse_pay(text: str) -> tuple[float, float] | None:
    """Devuelve (mín, máx) en dólares por hora, o None si no se sabe."""
    vals = []
    for num, k in MONEY.findall(text or ""):
        v = float(num.replace(",", ""))
        if k:
            v *= 1000
        if v < 7:  # no es un pago
            continue
        vals.append(v / 2080 if v >= 1000 else v)
    if not vals:
        return None
    return (min(vals), max(vals))
