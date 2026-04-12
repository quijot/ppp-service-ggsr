"""
parser.py — Extrae lat, lon, altura, incertidumbres y fecha del .sum de CSRS-PPP.

Formato v5.x (columnar). Líneas relevantes:

  MKR base
  BEG 2024-12-19 13:16:00.00
  POS LAT IGS20 24:354:53505   -25 34 24.21409   -25 34 24.24564   -0.97  0.0068  ...
  POS LON IGS20 24:354:53505   -64 58 16.52757   -64 58 16.47874    1.36  0.0078  ...
  POS HGT IGS20 24:354:53505          854.8828          850.9897   -3.89  0.0272  ...

Columnas (0-indexed tras split):
  0=POS  1=LAT/LON/HGT  2=sistema  3=época
  4-6 = a priori (DMS o metros)
  7-9 = estimado (DMS o metros)    <- lo que usamos
  10  = diferencia
  11  = sigma(95%) en metros       <- incertidumbre formal
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class PPPResult:
    lat: float  # grados decimales (negativo = Sur)
    lon: float  # grados decimales (negativo = Oeste)
    hgt: float  # altura elipsoidal en metros (IGS20)
    obs_date: str  # "yyyy-mm-dd"
    lat_dms: str  # "-25 34 24.24564"
    lon_dms: str  # "-64 58 16.47874"
    marker: str  # nombre del punto (campo MKR)
    # Incertidumbres formales sigma(95%) en metros
    sigma_lat: float = 0.0
    sigma_lon: float = 0.0
    sigma_hgt: float = 0.0


class SumParseError(Exception):
    pass


def dms_to_dd(dms_str: str) -> float:
    """Convierte '-25 34 24.24564' a grados decimales."""
    parts = dms_str.strip().split()
    if len(parts) != 3:
        raise SumParseError(f"Formato DMS inesperado: '{dms_str}'")
    deg = int(parts[0])
    mm = int(parts[1])
    ss = float(parts[2])
    sign = -1 if deg < 0 else 1
    return sign * (abs(deg) + mm / 60 + ss / 3600)


def _parse_pos_line(line: str) -> tuple[str, float]:
    """
    Parsea una línea POS LAT/LON/HGT.
    Retorna (valor_estimado_str, sigma_95).

    Para LAT/LON el estimado son 3 tokens DMS (7,8,9) y sigma es token 11.
    Para HGT el estimado es 1 token en metros (token 7) y sigma es token 9.
    """
    tokens = line.split()
    kind = tokens[1]  # LAT, LON o HGT

    if kind in ("LAT", "LON"):
        # tokens 7,8,9 = estimado DMS; token 11 = sigma(95%) en metros
        if len(tokens) < 12:
            raise SumParseError(f"Línea POS {kind} con formato inesperado: {line!r}")
        estimated = f"{tokens[7]} {tokens[8]} {tokens[9]}"
        sigma = float(tokens[11])
    else:  # HGT
        # tokens 4,5,6 son a priori (1 valor, pero puede venir con espacios extra)
        # Para HGT: token 7 = estimado en metros; token 9 = sigma
        if len(tokens) < 10:
            raise SumParseError(f"Línea POS HGT con formato inesperado: {line!r}")
        estimated = tokens[7]
        sigma = float(tokens[9])

    return estimated, sigma


def parse_sum(sum_path) -> PPPResult:
    """
    Lee un archivo .sum de CSRS-PPP v5.x.
    Extrae lat, lon, altura elipsoidal, incertidumbres formales, fecha y marker.
    Raises SumParseError si no puede encontrar los campos obligatorios.
    """
    lines = Path(sum_path).read_text(encoding="utf-8", errors="replace").splitlines()

    lat_dms = lon_dms = obs_date = marker = None
    hgt = 0.0
    sigma_lat = sigma_lon = sigma_hgt = 0.0

    for line in lines:
        s = line.strip()

        if s.startswith("MKR ") and marker is None:
            parts = s.split(maxsplit=1)
            marker = parts[1].strip() if len(parts) > 1 else ""

        # ── Fecha de inicio de observación ──────────────────────────
        # BEG 2024-12-19 13:16:00.00
        elif s.startswith("BEG ") and obs_date is None:
            parts = s.split()
            if len(parts) >= 2:
                try:
                    obs_date = datetime.strptime(parts[1], "%Y-%m-%d").strftime(
                        "%Y-%m-%d"
                    )
                except ValueError:
                    pass

        # ── Latitud estimada ─────────────────────────────────────────
        elif s.startswith("POS LAT ") and lat_dms is None:
            try:
                lat_dms, sigma_lat = _parse_pos_line(s)
            except (SumParseError, ValueError, IndexError):
                lat_dms, sigma_lat = (
                    s.split()[7:10:1][0] + " " + s.split()[8] + " " + s.split()[9],
                    0.0,
                )

        # ── Longitud estimada ────────────────────────────────────────
        elif s.startswith("POS LON ") and lon_dms is None:
            try:
                lon_dms, sigma_lon = _parse_pos_line(s)
            except (SumParseError, ValueError, IndexError):
                lon_dms, sigma_lon = (
                    s.split()[7] + " " + s.split()[8] + " " + s.split()[9],
                    0.0,
                )

        # ── Altura estimada ────────────────────────────────────────
        elif s.startswith("POS HGT ") and hgt == 0.0:
            try:
                hgt_str, sigma_hgt = _parse_pos_line(s)
                hgt = float(hgt_str)
            except (SumParseError, ValueError, IndexError):
                pass

        if lat_dms and lon_dms and obs_date and marker is not None and hgt != 0.0:
            break

    if not lat_dms:
        raise SumParseError("No se encontró POS LAT en el archivo .sum")
    if not lon_dms:
        raise SumParseError("No se encontró POS LON en el archivo .sum")
    if not hgt:
        raise SumParseError("No se encontró POS HGT en el archivo .sum")
    if not obs_date:
        raise SumParseError(
            "No se encontró BEG (fecha de observación) en el archivo .sum"
        )

    return PPPResult(
        lat=dms_to_dd(lat_dms),
        lon=dms_to_dd(lon_dms),
        hgt=hgt,
        obs_date=obs_date,
        lat_dms=lat_dms,
        lon_dms=lon_dms,
        marker=marker or "Punto",
        sigma_lat=sigma_lat,
        sigma_lon=sigma_lon,
        sigma_hgt=sigma_hgt,
    )
