"""
geodata_updater.py — Descarga y actualización de geodata desde fuentes externas.

Fuentes:
  - RAMSAC: API REST de IGN-Ar (GeoJSON o KML)
  - iws: FTP de IGN-Ar (archivos .crd diarios, agrupados por semana GPS)

Exporta:
  fetch_ramsac() -> dict
  fetch_iws_incremental(from_week, to_week, crd_dir, existing) -> dict
"""

import io
import json
import os
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pygeodesy as g

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
RAMSAC_API_BASE = (
    "https://dnsg.ign.gob.ar/apps/api/v1/capas-sig/"
    "Geodesia+y+demarcaci%C3%B3n/Redes+geod%C3%A9sicas/ramsac"
)
# Orden de preferencia de días al buscar la solución semanal
_WEEKDAY_PREFERENCE = [0, 6, 1, 5, 2, 4, 3]


# ---------------------------------------------------------------------------
# Conversión de coordenadas
# ---------------------------------------------------------------------------
def _xyz2lla(x: float, y: float, z: float) -> tuple[float, float, float]:
    r = g.EcefKarney(g.Datums.GRS80).reverse(x, y, z)
    return r.lat, r.lon, r.height


# ---------------------------------------------------------------------------
# RAMSAC
# ---------------------------------------------------------------------------
def _parse_geojson(data: dict) -> dict:
    ramsac = {}
    for st in data["features"]:
        lon, lat, *rest = st["geometry"]["coordinates"]
        ep = st["properties"]["codigo_estacion"]
        ramsac[ep] = {"lat": lat, "lon": lon}
        if rest and rest[0]:
            ramsac[ep]["alt"] = rest[0]
    return {ep: ramsac[ep] for ep in sorted(ramsac)}


def _parse_kml(kml_bytes: bytes) -> dict:
    """Parsea KML de IGN-Ar con xml.etree — sin dependencia de k2g."""
    ns = {
        "kml": "http://www.opengis.net/kml/2.2",
        "gx": "urn:schemas-google-com:kml:ext:2.2",
    }
    root = ET.fromstring(kml_bytes)
    ramsac = {}
    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        # Extraer código de estación de ExtendedData
        ep = None
        for sd in pm.iter("{http://www.opengis.net/kml/2.2}SimpleData"):
            if sd.get("name") == "codigo_estacion":
                ep = sd.text
                break
        if not ep:
            continue
        coords_el = pm.find(".//{http://www.opengis.net/kml/2.2}coordinates")
        if coords_el is None or not coords_el.text:
            continue
        parts = coords_el.text.strip().split(",")
        if len(parts) < 2:
            continue
        lon, lat = float(parts[0]), float(parts[1])
        ramsac[ep] = {"lat": lat, "lon": lon}
        if len(parts) >= 3 and parts[2].strip():
            ramsac[ep]["alt"] = float(parts[2])
    return {ep: ramsac[ep] for ep in sorted(ramsac)}


def fetch_ramsac() -> dict:
    """Descarga coordenadas POSGAR07 de las EPs RAMSAC desde IGN-Ar.

    Intenta primero el endpoint GeoJSON; si falla, descarga el ZIP con KML
    y lo parsea directamente con xml.etree (sin dependencia de k2g).
    """
    # Intento 1: endpoint GeoJSON
    try:
        with urlopen(f"{RAMSAC_API_BASE}/geojson", timeout=30) as resp:
            data = json.loads(resp.read())
        if data.get("features"):
            return _parse_geojson(data)
    except Exception:
        pass

    # Intento 2: ZIP con KML
    with urlopen(f"{RAMSAC_API_BASE}/kml", timeout=30) as resp:
        zip_bytes = resp.read()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        kml_name = next(n for n in zf.namelist() if n.endswith(".kml"))
        kml_bytes = zf.read(kml_name)

    return _parse_kml(kml_bytes)


# ---------------------------------------------------------------------------
# IGN-Ar weekly solutions (iws)
# ---------------------------------------------------------------------------
def _crd_filename(week: int, day: int) -> str:
    return f"ign{week}{day}.crd"


def _ftp_url(week: int, day: int) -> str:
    user = os.environ.get("IGN_FTP_USER")
    passwd = os.environ.get("IGN_FTP_PASS")
    if not user or not passwd:
        raise RuntimeError(
            "IGN_FTP_USER e IGN_FTP_PASS deben estar configurados como variables de entorno"
        )
    filename = _crd_filename(week, day)
    return f"ftp://{user}:{passwd}@ramsac.ign.gob.ar/{week}/{filename}"


def _read_crd(lines: list[str], result: dict) -> None:
    """Parsea líneas de un .crd IGN-Ar y agrega coordenadas a result."""
    for line in lines:
        if not line.strip()[:1].isdigit():
            continue
        try:
            ep = line[5:9].strip()
            x = float(line[20:36])
            y = float(line[37:51])
            z = float(line[52:66])
            lat, lon, alt = _xyz2lla(x, y, z)
            result[ep] = {"lat": lat, "lon": round(lon, 9), "alt": round(alt, 3)}
            result[ep]["lat"] = round(lat, 9)
        except (IndexError, ValueError):
            pass


def _get_week_solution(week: int, crd_dir: Path) -> dict:
    """Obtiene la solución para una semana GPS.

    Prueba los días en orden de preferencia. Si el .crd ya está en crd_dir
    lo usa directamente (caché). Si no, lo descarga del FTP y lo guarda.
    Combina todos los días disponibles de esa semana en un único dict.
    """
    crd_dir.mkdir(parents=True, exist_ok=True)
    week_result: dict = {}

    for day in _WEEKDAY_PREFERENCE:
        filename = _crd_filename(week, day)
        cached = crd_dir / filename

        if cached.exists():
            lines = cached.read_text(errors="replace").splitlines()
        else:
            url = _ftp_url(week, day)
            try:
                with urlopen(url, timeout=20) as resp:
                    content = resp.read().decode("utf-8", errors="replace")
                cached.write_text(content)
                lines = content.splitlines()
            except URLError:
                continue

        _read_crd(lines, week_result)

    return week_result


def fetch_iws_incremental(
    from_week: int,
    to_week: int,
    crd_dir: Path,
    existing: dict | None = None,
) -> dict:
    """Descarga soluciones IGN-Ar para semanas [from_week, to_week).

    Guarda cada .crd en crd_dir como caché para evitar re-descargas.
    Hace merge sobre existing si se provee.

    Returns:
        dict {gps_week: {ep: {lat, lon, alt}}}
    """
    iws: dict = dict(existing) if existing else {}

    total = to_week - from_week
    for i, wk in enumerate(range(from_week, to_week)):
        sol = _get_week_solution(wk, crd_dir)
        if sol:
            iws[wk] = sol
        if (i + 1) % 50 == 0:
            print(f"  iws: {i + 1}/{total} semanas procesadas...")

    return {wk: iws[wk] for wk in sorted(iws)}
