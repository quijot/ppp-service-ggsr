"""
tasks.py — Pipeline Celery: RINEX → NRCan CSRS-PPP → parseo .sum → POSGAR07.

Flujo:
  1. POST del RINEX a NRCan → keyid
  2. Polling hasta "done" (cada 10s, máx csrs_get_max intentos)
  3. Descarga y descompresión de full_output.zip
  4. Parseo del .sum (parser.py)
  5. Transformación ITRF→POSGAR07 (transform.py)
  6. Retorno de resultado estructurado a Redis

Tareas adicionales:
  update_geodata — descarga/actualiza ramsac + iws en Redis y en disco
"""

import json
import pickle
import shutil
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

import redis as redis_lib
import requests
from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready
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
    result_expires=3600,
    worker_max_tasks_per_child=10,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    beat_schedule={
        "update-geodata-weekly": {
            "task": "ppp_tasks.update_geodata",
            "schedule": crontab(hour=3, minute=0, day_of_week=2),  # martes 3am UTC
        }
    },
)

# ---------------------------------------------------------------------------
# NRCan domain
# ---------------------------------------------------------------------------
CSRS_DOMAIN = "https://webapp.csrs-scrs.nrcan-rncan.gc.ca"
CSRS_SUBMIT_URL = f"{CSRS_DOMAIN}/CSRS-PPP/service/submit"
CSRS_STATUS_URL = f"{CSRS_DOMAIN}/CSRS-PPP/service/results/status?id={{keyid}}"
CSRS_RESULT_URL = f"{CSRS_DOMAIN}/CSRS-PPP/service/results/file?id={{keyid}}"


# ---------------------------------------------------------------------------
# Helpers para sys.path (módulos ppp/)
# ---------------------------------------------------------------------------
def _ensure_ppp_in_path():
    if cfg.ppp_dir not in sys.path:
        sys.path.insert(0, cfg.ppp_dir)


# ---------------------------------------------------------------------------
# Helpers de geodata en Redis
# ---------------------------------------------------------------------------
def _geodata_to_redis(key: str, data) -> None:
    _redis.set(f"geodata:{key}", zlib.compress(pickle.dumps(data)))


def _geodata_from_redis(key: str):
    raw = _redis.get(f"geodata:{key}")
    return pickle.loads(zlib.decompress(raw)) if raw else None


def _save_pickle(filename: str, data) -> None:
    path = Path(cfg.data_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(data))


# ---------------------------------------------------------------------------
# Tarea: update_geodata
# ---------------------------------------------------------------------------
@celery_app.task(bind=True, name="ppp_tasks.update_geodata")
def update_geodata(self, full: bool = False):
    """Descarga RAMSAC + iws de IGN-Ar, actualiza Redis y los pickles locales.

    full=True  → bootstrap completo desde semana 1388.
    full=False → solo semanas nuevas desde geodata:last_week (incremental).
    """
    from app.geodata_updater import fetch_ramsac, fetch_iws_incremental
    from gnsstime import gnsstime as gt

    _redis.set("geodata:updating", "1")
    try:
        # --- RAMSAC ---
        self.update_state(state="PROGRESS", meta={"step": "ramsac"})
        ramsac = fetch_ramsac()
        _geodata_to_redis("ramsac", ramsac)
        _save_pickle("ramsac.pickle", ramsac)

        # --- iws ---
        last_raw = _redis.get("geodata:last_week")
        if last_raw:
            last_raw = last_raw.decode() if isinstance(last_raw, bytes) else last_raw

        if full or not last_raw:
            from_week, existing = 1388, {}
        else:
            from_week = int(last_raw) + 1
            existing = _geodata_from_redis("iws") or {}

        to_week = gt.now().gpsw + 1
        if from_week < to_week:
            self.update_state(
                state="PROGRESS",
                meta={"step": "iws", "from_week": from_week, "to_week": to_week - 1},
            )
            crd_dir = Path(cfg.data_dir) / "iws_crd"
            iws = fetch_iws_incremental(from_week, to_week, crd_dir, existing)
            _geodata_to_redis("iws", iws)
            _save_pickle("iws.pickle", iws)
            _redis.set("geodata:last_week", str(max(iws)))

        return {"status": "ok", "last_week": str(_redis.get("geodata:last_week") or "")}

    finally:
        _redis.delete("geodata:updating")


# ---------------------------------------------------------------------------
# Señal worker_ready: bootstrap automático si Redis está vacío
# ---------------------------------------------------------------------------
@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    if not _redis.exists("geodata:ramsac"):
        update_geodata.apply_async(kwargs={"full": True})


# ---------------------------------------------------------------------------
# _run_transform
# ---------------------------------------------------------------------------
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
    """Transforma coordenadas PPP ITRF → POSGAR07 y construye el resultado."""
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
# Tarea principal: process_rinex
# ---------------------------------------------------------------------------
@celery_app.task(bind=True, name="ppp_tasks.process_rinex")
def process_rinex(self, job_id: str, rinex_filename: str):
    """Pipeline completo: RINEX → NRCan → .sum → POSGAR07."""

    def _update(status: str, msg: str = ""):
        self.update_state(state=status, meta={"msg": msg})

    # ------------------------------------------------------------------
    # Preflight: verificar disponibilidad de geodata
    # ------------------------------------------------------------------
    geodata_en_redis = _redis.exists("geodata:ramsac")
    geodata_en_disco = (Path(cfg.data_dir) / "ramsac.pickle").exists()
    if not geodata_en_redis and not geodata_en_disco:
        en_proceso = _redis.exists("geodata:updating")
        msg = (
            "Datos geodésicos en inicialización (~20 min en el primer arranque). "
            "Reintentá en unos minutos."
            if en_proceso
            else "Datos geodésicos no disponibles. Contactá al administrador."
        )
        raise RuntimeError(msg)

    # ------------------------------------------------------------------
    # Recuperar RINEX desde Redis
    # ------------------------------------------------------------------
    rinex_bytes = _redis.get(f"rinex:{job_id}")
    if not rinex_bytes:
        raise RuntimeError(
            "Archivo RINEX no encontrado en Redis (expiró o nunca se subió)."
        )
    _redis.delete(f"rinex:{job_id}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="ppp_"))
    rinex_file = tmp_dir / rinex_filename
    rinex_file.write_bytes(rinex_bytes)
    rinex_path = str(rinex_file)

    work_dir = Path(cfg.results_dir) / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

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
        # NRCan devuelve errors.zip dentro del full_output.zip cuando el RINEX es inválido
        nrcan_error = None
        for errors_zip in work_dir.rglob("errors.zip"):
            try:
                with zipfile.ZipFile(errors_zip, "r") as ez:
                    if "errors.txt" in ez.namelist():
                        nrcan_error = (
                            ez.read("errors.txt")
                            .decode("utf-8", errors="replace")
                            .strip()
                        )
                        break
            except zipfile.BadZipFile:
                pass
        if nrcan_error:
            raise RuntimeError(f"NRCan rechazó el archivo RINEX:\n{nrcan_error}")
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
