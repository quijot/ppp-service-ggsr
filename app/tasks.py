"""
tasks.py — Pipeline Celery: RINEX → NRCan CSRS-PPP → parseo .sum → POSGAR07.

Flujo:
  1. POST del RINEX a NRCan → keyid
  2. Polling hasta "done" (cada 10s, máx csrs_get_max intentos)
  3. Descarga y descompresión de full_output.zip
  4. Parseo del .sum (parser.py)
  5. Transformación ITRF→POSGAR07 (transform.py)
  6. Retorno de resultado estructurado a Redis
"""

import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import redis as redis_lib
import requests
from celery import Celery
from requests_toolbelt.multipart.encoder import MultipartEncoder

from app.config import get_settings
from app.parser import parse_sum, SumParseError

cfg = get_settings()
_redis = redis_lib.from_url(cfg.redis_url, decode_responses=False)

# ---------------------------------------------------------------------------
# Celery app
# ---------------------------------------------------------------------------
celery_app = Celery(
    "ppp_tasks",
    broker=cfg.redis_url,
    backend=cfg.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=3600,  # resultados expiran en 1 hora
    worker_max_tasks_per_child=10,  # evita memory leaks en workers largos
)

# ---------------------------------------------------------------------------
# NRCan domain
# ---------------------------------------------------------------------------
CSRS_DOMAIN = "https://webapp.csrs-scrs.nrcan-rncan.gc.ca"
CSRS_SUBMIT_URL = f"{CSRS_DOMAIN}/CSRS-PPP/service/submit"
CSRS_STATUS_URL = f"{CSRS_DOMAIN}/CSRS-PPP/service/results/status?id={{keyid}}"
CSRS_RESULT_URL = f"{CSRS_DOMAIN}/CSRS-PPP/service/results/file?id={{keyid}}"


# ---------------------------------------------------------------------------
# Helpers para agregar el path de ppp/ al sys.path y usar calc2 como módulo
# ---------------------------------------------------------------------------
def _ensure_ppp_in_path():
    if cfg.ppp_dir not in sys.path:
        sys.path.insert(0, cfg.ppp_dir)


def _run_transform(
    lat: float,
    lon: float,
    hgt: float,
    obs_date: str,
    marker: str = "Punto",
    ref_frame: str = "ITRF",
    sigma_lat: float = 0.0,
    sigma_lon: float = 0.0,
    sigma_hgt: float = 0.0,
) -> dict:
    """
    Transforma coordenadas PPP ITRF → POSGAR07 y construye el resultado.
    La altura elipsoidal se reporta en IGS20 sin transformar (ramsac aún no
    tiene coordenadas altimétricas de referencia POSGAR07).
    """
    _ensure_ppp_in_path()

    from gnsstime import gnsstime as gt
    from geodata import iws, ramsac
    from transform import transform_itrf_to_posgar07, dd2dms
    from geojson import Feature, FeatureCollection, LineString, Point

    obs_dt = gt.strptime(obs_date, "%Y-%m-%d")
    obs_wk = obs_dt.gpsw

    # --- Transformación principal ---
    res = transform_itrf_to_posgar07(lat, lon, obs_wk, iws, ramsac)

    lat_posgar_dms = dd2dms(res.lat)
    lon_posgar_dms = dd2dms(res.lon)
    lat_ppp_dms = dd2dms(lat)
    lon_ppp_dms = dd2dms(lon)

#     # Descripción de calidad del resultado
#     if res.cv_error_cm < 0:
#         quality_str = "Error CV no disponible (pocas EP)"
#         quality_class = "warn"
#     elif res.cv_error_cm < 5:
#         quality_str = f"Buena ({res.cv_error_cm:.1f} cm CV)"
#         quality_class = "good"
#     elif res.cv_error_cm < 10:
#         quality_str = f"Moderada ({res.cv_error_cm:.1f} cm CV)"
#         quality_class = "warn"
#     else:
#         quality_str = f"Baja ({res.cv_error_cm:.1f} cm CV)"
#         quality_class = "poor"

#     nearest_lines = [f"{ep}: {dist:.1f} km" for ep, dist in res.ep_nearest.items()]

#     result_html = f"""
# <div class="result-block">
#   <h4>Resultado POSGAR07</h4>
#   <p class="coords-lead"><strong>{lat_posgar_dms}, {lon_posgar_dms}</strong></p>
#   <p class="quality quality-{quality_class}">Calidad estimada: {quality_str}</p>
# </div>
# <hr>
# <div class="report-block">
#   <h4>Reporte</h4>
#   <p>
#     <strong>PPP results</strong> (semana GPS {obs_wk}):<br>
#     &nbsp;&nbsp;{lat:.10f}, {lon:.10f}<br>
#     &nbsp;&nbsp;{lat_ppp_dms}, {lon_ppp_dms}
#   </p>
#   <p>
#     <strong>Transformación IDW</strong>
#     (semana {res.wk_used}, n={res.n_used}, p={res.p_used},
#     radio={res.radius_km:.0f} km, {res.n_ep_cv} EP en CV)<br>
#     &nbsp;&nbsp;EP usadas:<br>
#     &nbsp;&nbsp;&nbsp;&nbsp;{"<br>&nbsp;&nbsp;&nbsp;&nbsp;".join(nearest_lines)}<br>
#     &nbsp;&nbsp;{res.lat:.15f}, {res.lon:.15f}<br>
#     &nbsp;&nbsp;<strong>{lat_posgar_dms}, {lon_posgar_dms}</strong>
#   </p>
# </div>
# """

    # --- GeoJSON para Leaflet ---
    point_desc = "<b>Coordenadas POSGAR07</b><br><b>lat:</b> {}<br><b>lon:</b> {}"
    features = [
        Feature(
            geometry=Point([res.lon, res.lat]),
            properties={
                "name": marker.upper(),
                "description": point_desc.format(lat_posgar_dms, lon_posgar_dms),
                "color": "rgba(220, 38, 38, 0.85)",
                "is_base": True,
            },
        )
    ]
    for ep, dist_km in res.ep_nearest.items():
        ep_lat = ramsac[ep]["lat"]
        ep_lon = ramsac[ep]["lon"]
        features.append(
            Feature(
                geometry=Point([ep_lon, ep_lat]),
                properties={
                    "name": ep,
                    "description": f"<b>{ep}</b><br>dist: {dist_km:.1f} km",
                    "color": "rgba(37, 99, 235, 0.85)",
                },
            )
        )
        features.append(
            Feature(
                geometry=LineString([(res.lon, res.lat), (ep_lon, ep_lat)]),
                properties={
                    "name": f"BASE-{ep}",
                    "description": f"<b>distancia</b>: {dist_km:.1f} km",
                    "color": "#16a34a",
                },
            )
        )

    geojson = FeatureCollection(features)

    return {
        "geojson": dict(geojson),
        "lat": res.lat,
        "lon": res.lon,
        "hgt": hgt,  # altura IGS20, sin transformar
        "marker": marker,
        "lat_posgar_dms": lat_posgar_dms,
        "lon_posgar_dms": lon_posgar_dms,
        "lat_ppp_dms": lat_ppp_dms,
        "lon_ppp_dms": lon_ppp_dms,
        "hgt_str": f"{hgt:.4f}",
        "obs_wk": obs_wk,
        "cv_error_cm": res.cv_error_cm,
        "cv_error_lat_cm": res.cv_error_lat_cm,
        "cv_error_lon_cm": res.cv_error_lon_cm,
        "n_used": res.n_used,
        "p_used": res.p_used,
        "n_ep_cv": res.n_ep_cv,
        "wk_used": res.wk_used,
        "radius_km": res.radius_km,
        "ep_nearest": res.ep_nearest,
        # Incertidumbres PPP (sigma 95%) en metros
        "sigma_lat": sigma_lat,
        "sigma_lon": sigma_lon,
        "sigma_hgt": sigma_hgt,
        "ref_frame": ref_frame,
    }


# ---------------------------------------------------------------------------
# Tarea principal
# ---------------------------------------------------------------------------
@celery_app.task(bind=True, name="ppp_tasks.process_rinex")
def process_rinex(self, job_id: str, rinex_filename: str):
    """
    Pipeline completo: RINEX → NRCan → .sum → POSGAR07.
    El estado se va actualizando en Redis via self.update_state().
    El contenido del RINEX se recupera de Redis (clave rinex:{job_id}).
    """

    def _update(status: str, msg: str = ""):
        self.update_state(state=status, meta={"msg": msg})

    # Recuperar bytes del RINEX desde Redis y escribir a un temp file local
    rinex_bytes = _redis.get(f"rinex:{job_id}")
    if not rinex_bytes:
        raise RuntimeError("Archivo RINEX no encontrado en Redis (expiró o nunca se subió).")
    _redis.delete(f"rinex:{job_id}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="ppp_"))
    rinex_file = tmp_dir / rinex_filename
    rinex_file.write_bytes(rinex_bytes)
    rinex_path = str(rinex_file)

    work_dir = Path(cfg.results_dir) / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    rinex_name = rinex_file.stem

    # ------------------------------------------------------------------
    # 1. Enviar a NRCan
    # ------------------------------------------------------------------
    _update("SUBMITTING", "Enviando RINEX a NRCan CSRS-PPP...")

    content = {
        "return_email": "dummy_email",
        "cmd_process_type": "std",
        "ppp_access": "nobrowser_status",
        "language": "en",
        "user_name": cfg.csrs_user_email,
        "process_type": cfg.csrs_mode,
        "sysref": cfg.csrs_ref,
        "nad83_epoch": "CURR",
        "v_datum": "cgvd2013",
        "rfile_upload": (rinex_file.name, open(rinex_path, "rb"), "text/plain"),
        "output_pdf": "lite",
    }

    keyid = None
    for attempt in range(5):
        mtp = MultipartEncoder(fields=content)
        headers = {
            "User-Agent": "CSRS-PPP access via Python Browser Emulator",
            "Content-Type": mtp.content_type,
            "Accept": "text/plain",
        }
        try:
            resp = requests.post(CSRS_SUBMIT_URL, data=mtp, headers=headers, timeout=30)
            keyid = resp.text.strip()
            if keyid and "DOCTYPE" not in keyid and "ERROR" not in keyid:
                break
            # Reintentar con nuevo encoder (el stream se consumió)
            content["rfile_upload"] = (
                rinex_file.name,
                open(rinex_path, "rb"),
                "text/plain",
            )
            mtp = MultipartEncoder(fields=content)
            headers["Content-Type"] = mtp.content_type
            time.sleep(5)
        except requests.RequestException as e:
            if attempt == 4:
                raise RuntimeError(f"No se pudo conectar con NRCan: {e}")
            time.sleep(10)

    if not keyid:
        raise RuntimeError("NRCan no devolvió un keyid válido.")

    # ------------------------------------------------------------------
    # 2. Polling de estado
    # ------------------------------------------------------------------
    _update("PROCESSING", f"NRCan procesando (keyid={keyid})...")

    for poll_num in range(cfg.csrs_get_max):
        time.sleep(10)
        try:
            r = requests.get(CSRS_STATUS_URL.format(keyid=keyid), timeout=10)
            status_text = r.text.lower()
        except requests.RequestException:
            continue

        if "done" in status_text:
            break
        elif "error" in status_text:
            raise RuntimeError(
                f"NRCan reportó error en el procesamiento (keyid={keyid})"
            )

        _update(
            "PROCESSING",
            f"NRCan procesando... ({poll_num + 1}/{cfg.csrs_get_max} checks)",
        )
    else:
        raise RuntimeError(
            "Tiempo máximo de espera agotado esperando respuesta de NRCan."
        )

    # ------------------------------------------------------------------
    # 3. Descargar y descomprimir full_output.zip
    # ------------------------------------------------------------------
    _update("DOWNLOADING", "Descargando resultados de NRCan...")

    zip_path = work_dir / "full_output.zip"
    r = requests.get(CSRS_RESULT_URL.format(keyid=keyid), timeout=30)
    zip_path.write_bytes(r.content)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(work_dir)
    except zipfile.BadZipFile:
        raise RuntimeError("El archivo descargado de NRCan no es un ZIP válido.")

    # Encontrar el .sum (puede estar en subdirectorio)
    sum_files = list(work_dir.rglob("*.sum"))
    if not sum_files:
        raise RuntimeError("No se encontró archivo .sum en los resultados de NRCan.")
    sum_path = sum_files[0]

    # ------------------------------------------------------------------
    # 4. Parsear .sum
    # ------------------------------------------------------------------
    _update("PARSING", "Parseando resultados PPP...")

    try:
        ppp = parse_sum(sum_path)
    except SumParseError as e:
        raise RuntimeError(f"Error al parsear .sum: {e}")

    # ------------------------------------------------------------------
    # 5. Transformar ITRF → POSGAR07
    # ------------------------------------------------------------------
    _update("TRANSFORMING", "Transformando ITRF → POSGAR07...")

    calc_result = _run_transform(
        lat=ppp.lat,
        lon=ppp.lon,
        hgt=ppp.hgt,
        obs_date=ppp.obs_date,
        marker=ppp.marker,
        ref_frame=ppp.ref_frame,
        sigma_lat=ppp.sigma_lat,
        sigma_lon=ppp.sigma_lon,
        sigma_hgt=ppp.sigma_hgt,
    )

    # Serializar GeoJSON (es un objeto geojson, no un dict puro)
    geojson_dict = json.loads(json.dumps(calc_result["geojson"]))

    # ------------------------------------------------------------------
    # Limpiar archivos temporales
    # ------------------------------------------------------------------
    try:
        shutil.rmtree(work_dir)
        shutil.rmtree(tmp_dir)
    except Exception:
        pass

    return {
        "status": "done",
        "geojson": geojson_dict,
        "lat": calc_result["lat"],
        "lon": calc_result["lon"],
        "hgt": calc_result["hgt"],
        "marker": calc_result["marker"],
        "lat_posgar_dms": calc_result["lat_posgar_dms"],
        "lon_posgar_dms": calc_result["lon_posgar_dms"],
        "lat_ppp_dms": calc_result["lat_ppp_dms"],
        "lon_ppp_dms": calc_result["lon_ppp_dms"],
        "hgt_str": calc_result["hgt_str"],
        "obs_wk": calc_result["obs_wk"],
        "cv_error_cm": calc_result["cv_error_cm"],
        "cv_error_lat_cm": calc_result["cv_error_lat_cm"],
        "cv_error_lon_cm": calc_result["cv_error_lon_cm"],
        "n_used": calc_result["n_used"],
        "p_used": calc_result["p_used"],
        "n_ep_cv": calc_result["n_ep_cv"],
        "wk_used": calc_result["wk_used"],
        "radius_km": calc_result["radius_km"],
        "ep_nearest": calc_result["ep_nearest"],
        "sigma_lat": calc_result["sigma_lat"],
        "sigma_lon": calc_result["sigma_lon"],
        "sigma_hgt": calc_result["sigma_hgt"],
        "ref_frame": calc_result["ref_frame"],
        "error_msg": "",
    }
