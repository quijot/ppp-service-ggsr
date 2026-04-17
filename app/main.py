"""
main.py — FastAPI app del Servicio PPP Experimental GGSR.

Endpoints:
  GET  /                    → formulario principal (dos pestañas)
  POST /upload              → recibe RINEX, lanza tarea Celery
  GET  /job/{job_id}        → página de seguimiento
  GET  /api/status/{job_id} → polling JSON del estado
  POST /api/transform       → transformación directa ITRF→POSGAR07 (sin RINEX)
  GET  /como-funciona       → documentación técnica
  GET  /health              → healthcheck
"""

import uuid

import redis as redis_lib
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from celery.result import AsyncResult

from app.config import get_settings
from app.tasks import celery_app, process_rinex

cfg = get_settings()

app = FastAPI(title="Servicio PPP Experimental GGSR", version="1.0.0")
templates = Jinja2Templates(directory="app/templates")
_redis = redis_lib.from_url(cfg.redis_url, decode_responses=False)

# ---------------------------------------------------------------------------
# Extensiones RINEX permitidas
# ---------------------------------------------------------------------------
ALLOWED_EXTENSIONS = {
    ".obs",
    ".rnx",
    ".gz",
    ".zip",
    ".Z",
    ".??o",
    "??d",
    # formatos RINEX 2 con extensión de año: .15o, .24o, etc.
}


def _is_rinex_allowed(filename: str) -> bool:
    name = filename.lower()
    # Extensión explícita permitida
    for ext in ALLOWED_EXTENSIONS:
        if name.endswith(ext.lower()):
            return True
    # RINEX 2: termina en dígito + "o" o "d" (ej: .25o, .24d)
    if len(name) >= 3 and name[-1] in ("o", "d") and name[-3].isdigit():
        return True
    return False


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/como-funciona", response_class=HTMLResponse)
async def como_funciona(request: Request):
    return templates.TemplateResponse("como_funciona.html", {"request": request})


@app.get("/job/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str):
    return templates.TemplateResponse(
        "index.html", {"request": request, "job_id": job_id}
    )


# ---------------------------------------------------------------------------
# Upload RINEX
# ---------------------------------------------------------------------------


@app.post("/upload")
async def upload_rinex(request: Request, rinex_file: UploadFile = File(...)):
    if not _is_rinex_allowed(rinex_file.filename or ""):
        raise HTTPException(status_code=400, detail="Archivo no válido.")

    contents = await rinex_file.read()
    if len(contents) > 20 * 1024 * 1024:
        raise HTTPException(
            status_code=400, detail="El archivo supera el límite de 20 MB."
        )

    job_id = str(uuid.uuid4())
    _redis.set(f"rinex:{job_id}", contents, ex=3600)

    process_rinex.apply_async(
        args=[job_id, rinex_file.filename or "upload.rnx"], task_id=job_id
    )
    return RedirectResponse(url=f"/job/{job_id}", status_code=303)


# ---------------------------------------------------------------------------
# Polling de estado
# ---------------------------------------------------------------------------


@app.get("/api/status/{job_id}")
async def job_status(job_id: str):
    result = AsyncResult(job_id, app=celery_app)
    state = result.state

    if state == "PENDING":
        return JSONResponse({"status": "pending", "msg": "En cola..."})

    if state == "FAILURE":
        return JSONResponse({"status": "error", "msg": str(result.result)})

    if state == "SUCCESS":
        res = result.result
        return JSONResponse(
            {
                "status": "done",
                "geojson": res.get("geojson", {}),
                "lat": res.get("lat"),
                "lon": res.get("lon"),
                "hgt": res.get("hgt"),
                "marker": res.get("marker", "Punto"),
                "lat_posgar_dms": res.get("lat_posgar_dms", ""),
                "lon_posgar_dms": res.get("lon_posgar_dms", ""),
                "lat_ppp_dms": res.get("lat_ppp_dms", ""),
                "lon_ppp_dms": res.get("lon_ppp_dms", ""),
                "hgt_str": res.get("hgt_str", ""),
                "obs_wk": res.get("obs_wk"),
                "cv_error_cm": res.get("cv_error_cm", -1),
                "cv_error_lat_cm": res.get("cv_error_lat_cm", -1),
                "cv_error_lon_cm": res.get("cv_error_lon_cm", -1),
                "n_used": res.get("n_used"),
                "p_used": res.get("p_used"),
                "n_ep_cv": res.get("n_ep_cv"),
                "wk_used": res.get("wk_used"),
                "radius_km": res.get("radius_km"),
                "ep_nearest": res.get("ep_nearest", {}),
                "sigma_lat": res.get("sigma_lat", 0),
                "sigma_lon": res.get("sigma_lon", 0),
                "sigma_hgt": res.get("sigma_hgt", 0),
                "ref_frame": res.get("ref_frame", "ITRF"),
            }
        )

    meta = result.info or {}
    return JSONResponse({"status": "processing", "msg": meta.get("msg", state)})


# ---------------------------------------------------------------------------
# Transformación directa ITRF → POSGAR07 (sin RINEX)
# ---------------------------------------------------------------------------


@app.post("/api/transform")
async def transform_direct(
    lat: float = Form(...),
    lon: float = Form(...),
    hgt: float = Form(0.0),
    date: str = Form(...),  # "yyyy-mm-dd"
):
    """
    Transforma coordenadas ITRF/IGS al marco POSGAR07 sin procesar un RINEX.
    Útil para quien ya tiene coordenadas PPP de otro origen.
    """
    import sys

    sys.path.insert(0, cfg.ppp_dir)

    try:
        from gnsstime import gnsstime as gt
        from geodata import iws, ramsac
        from transform import transform_itrf_to_posgar07, dd2dms

        obs_dt = gt.strptime(date, "%Y-%m-%d")
        obs_wk = obs_dt.gpsw

        res = transform_itrf_to_posgar07(lat, lon, obs_wk, iws, ramsac)

        return JSONResponse(
            {
                "status": "done",
                "lat": res.lat,
                "lon": res.lon,
                "hgt": hgt,  # altura sin transformar
                "lat_posgar_dms": dd2dms(res.lat),
                "lon_posgar_dms": dd2dms(res.lon),
                "lat_input_dms": dd2dms(lat),
                "lon_input_dms": dd2dms(lon),
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
                "ref_frame": "IGS20",  # transform directo asume IGS20
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {"ok": True}
