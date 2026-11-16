"""
geodata.py — Carga los datos geodésicos RAMSAC e iws.

Orden de carga (fallback en cascada):
  1. Redis  (geodata:ramsac / geodata:iws) — compartido entre web y worker
  2. Pickle en disco (/app/ppp/*.pickle)   — fallback local o sin Redis
  3. FileNotFoundError con mensaje claro

Uso:
  from geodata import get_geodata
  iws, ramsac = get_geodata()

get_geodata() cachea los dicts en memoria y solo los recarga cuando cambió la
fuente: el sello `geodata:version` en Redis (lo escribe update_geodata) o el
mtime de los pickles en disco. La verificación y la recarga van bajo un lock,
así que es seguro llamarla desde varios threads (worker con --pool=threads,
web con uvicorn).

  ramsac : {ep: {lat, lon[, alt]}}             — coordenadas POSGAR07
  iws    : {gps_week: {ep: {lat, lon, alt}}}   — soluciones semanales IGN-Ar
"""

import os
import pickle
import threading
import zlib
from pathlib import Path

_dir = Path(os.environ.get("PPP_DATA_DIR") or Path(__file__).parent)
_redis_url = os.environ.get("REDIS_URL")
_redis = None
if _redis_url:
    try:
        import redis as redis_lib

        _redis = redis_lib.from_url(_redis_url, decode_responses=False)
    except Exception:
        _redis = None

_PICKLES = {"ramsac": "ramsac.pickle", "iws": "iws.pickle"}

_lock = threading.Lock()
_loaded_version = None
ramsac: dict | None = None
iws: dict | None = None


def _from_redis(key: str):
    if _redis is None:
        return None
    try:
        raw = _redis.get(f"geodata:{key}")
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
            "El worker puede estar inicializando los datos (~2 hs desde su inicio)."
        )
    return data


def _source_version():
    """Huella de las fuentes: sello en Redis + mtime de los pickles.

    Es barata (un GET chico y dos stat) comparada con recargar ~MB de pickles.
    """
    stamp = None
    if _redis is not None:
        try:
            stamp = _redis.get("geodata:version")
        except Exception:
            stamp = None
    mtimes = []
    for filename in _PICKLES.values():
        try:
            mtimes.append((_dir / filename).stat().st_mtime_ns)
        except OSError:
            mtimes.append(None)
    return (stamp, tuple(mtimes))


def get_geodata():
    """Devuelve (iws, ramsac). Recarga solo si cambió la fuente desde la última carga."""
    global _loaded_version, ramsac, iws

    with _lock:
        version = _source_version()
        if ramsac is None or iws is None or version != _loaded_version:
            new_ramsac = _load("ramsac", _PICKLES["ramsac"])
            new_iws = _load("iws", _PICKLES["iws"])
            ramsac, iws = new_ramsac, new_iws
            _loaded_version = version
        return iws, ramsac
