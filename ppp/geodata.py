"""
geodata.py — Carga los datos geodésicos RAMSAC e iws.

Orden de carga (fallback en cascada):
  1. Redis  (geodata:ramsac / geodata:iws) — compartido entre web y worker
  2. Pickle en disco (/app/ppp/*.pickle)   — fallback local o sin Redis
  3. FileNotFoundError con mensaje claro

Exporta:
  ramsac : {ep: {lat, lon}}          — coordenadas POSGAR07
  iws    : {gps_week: {ep: {lat, lon, alt}}}  — soluciones semanales IGN-Ar
"""

import os
import pickle
import zlib
from pathlib import Path

_dir = Path(os.environ.get("PPP_DATA_DIR") or Path(__file__).parent)
_redis_url = os.environ.get("REDIS_URL")


def _from_redis(key: str):
    if not _redis_url:
        return None
    try:
        import redis as redis_lib

        r = redis_lib.from_url(_redis_url, decode_responses=False)
        raw = r.get(f"geodata:{key}")
        return pickle.loads(zlib.decompress(raw)) if raw else None
    except Exception:
        return None


def _from_pickle(filename: str):
    path = _dir / filename
    if path.exists():
        return pickle.loads(path.read_bytes())
    return None


def _load(key: str, filename: str):
    data = _from_redis(key) or _from_pickle(filename)
    if data is None:
        raise FileNotFoundError(
            f"Geodata '{key}' no disponible. "
            "Redis vacío y pickle no encontrado. "
            "El worker puede estar inicializando los datos (~20 min)."
        )
    return data


ramsac = _load("ramsac", "ramsac.pickle")
iws = _load("iws", "iws.pickle")
