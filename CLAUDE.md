# PPP Service GGSR — Contexto para Claude Code

## Qué es este proyecto

Servicio web del **Grupo de Geodesia Satelital de Rosario (GGSR)** que:
1. Recibe un archivo RINEX del usuario
2. Lo envía a la API de NRCan CSRS-PPP (Canadian Geodetic Survey)
3. Parsea el resultado (.sum) para obtener coordenadas en IGS20/ITRF
4. Transforma esas coordenadas al marco oficial argentino **POSGAR07** (ITRF2005, época 2006.632)
5. Muestra el resultado con mapa interactivo, métricas de calidad y documentación

## Stack

- **Backend**: FastAPI + Celery + Redis
- **Deploy**: Railway (web + worker como servicios separados)
- **Frontend**: HTML/JS puro + Leaflet.js
- **Python**: 3.11, gestionado con `requirements.txt` + Docker tanto en desarrollo como en producción
- **Linting/formato**: `ruff` (reemplaza flake8/black/isort) — config en `ruff.toml`, instalar con `requirements-dev.txt`
- **OS del desarrollador**: Arch Linux

## Estructura del proyecto

```
ppp-service-ggsr/
├── app/
│   ├── main.py              # FastAPI: endpoints /upload /job/{id} /api/status/{id}
│   │                        #          /api/transform /como-funciona /health
│   ├── tasks.py             # Celery worker: pipeline RINEX→NRCan→parseo→POSGAR07
│   ├── parser.py            # Parser del .sum de NRCan v5.x (formato columnar)
│   ├── config.py            # Settings via pydantic-settings (.env)
│   └── templates/
│       ├── index.html           # Frontend: dos pestañas (PPP completo / transformación directa)
│       └── como_funciona.html   # Documentación técnica pública
├── ppp/
│   ├── transform.py         # ⭐ Módulo central: transformación IGS20→POSGAR07 con CV-LOO IDW
│   ├── geodata.py           # Carga pickles con rutas absolutas (ramsac, iws, sws)
│   ├── calc.py              # Script original — referencia, no usado en el pipeline activo
│   ├── itrf2posgar.py       # Módulo original — referencia, no usado en el pipeline activo
│   ├── ramsac.pickle        # ⚠ NO en git — coordenadas POSGAR07 de 127 EP de RAMSAC
│   ├── iws.pickle           # ⚠ NO en git — soluciones semanales IGS20 de las EP (1007 semanas)
│   └── sws.pickle           # ⚠ NO en git — soluciones semanales alternativas
├── .env.example
├── .gitignore
├── CLAUDE.md                # este archivo
├── Dockerfile
├── Procfile
├── README.md
├── docker-compose.yml       # desarrollo local: redis + web + worker + flower
├── railway.toml             # configuración de build/deploy Railway (servicio web)
└── requirements.txt
```

## Pipeline completo

```
Usuario sube RINEX
      ↓
POST /upload → guarda bytes en Redis (rinex:{job_id}, TTL 1h), lanza tarea Celery con job_id
      ↓
Celery worker (tasks.py):
  0. Lee bytes del RINEX desde Redis → escribe a tempfile local
  1. POST RINEX a NRCan CSRS-PPP → recibe keyid
  2. Polling /status?id=keyid cada 10s hasta "done" (máx CSRS_GET_MAX × 10s)
  3. Descarga full_output.zip → extrae .sum
  4. parser.py → PPPResult (lat, lon, hgt, ref_frame, sigmas, marker, fecha)
  5. transform.py → TransformResult (lat/lon POSGAR07 + métricas CV)
  6. Guarda resultado estructurado en Redis
      ↓
Frontend JS hace polling GET /api/status/{job_id} cada 5s
      ↓
Cuando status="done" → renderiza resultado + mapa Leaflet
```

## Decisiones de diseño importantes

### parser.py — formato .sum de NRCan v5.x

El formato cambió completamente en v5.x: ahora es columnar con prefijos de 3 letras.

Líneas relevantes:
```
MKR base                          ← nombre del punto
BEG 2024-12-19 13:16:00.00        ← fecha de inicio de observaciones
POS LAT IGS20 24:354:53505   -25 34 24.21409   -25 34 24.24564   -0.97  0.0068  ...
POS LON IGS20 24:354:53505   -64 58 16.52757   -64 58 16.47874    1.36  0.0078  ...
POS HGT IGS20 24:354:53505          854.8828          850.9897   -3.89  0.0272  ...
```

- Token 2 = marco de referencia (`IGS20`, `IGS14`, etc.) → `PPPResult.ref_frame`
- Tokens 7-8-9 = valor estimado DMS (LAT/LON) → valor usado
- Token 5 = valor estimado metros (HGT)
- Token 11 = sigma(95%) en metros (LAT/LON), token 7 para HGT
- `MKR` = nombre del punto → se muestra como etiqueta en el mapa
- `BEG` = fecha de inicio (NO existe "OBS DATE" en v5.x)

**Marco de referencia**: NRCan v5 con Repro3 usa IGS20 para datos recientes
(post-2022 aprox.) e IGS14 para históricos. La diferencia es <1 cm para Argentina.
El parser lo captura y propaga — nunca hardcodear "IGS20".

### transform.py — transformación IGS20→POSGAR07

**NO usar** `get_best_configuration()` de `itrf2posgar.py` (evaluaba sobre 1 EP, inestable).
**SÍ usar** `transform_itrf_to_posgar07()` de `transform.py`.

Algoritmo:
1. Calcular `delta(EP, wk) = iws[wk][EP] - ramsac[EP]` para cada EP disponible
   (absorbe empíricamente: deriva tectónica + diferencia de marcos + sismos + efectos locales)
2. Filtrar outliers con MAD (Median Absolute Deviation) — más robusto que std
3. Radio adaptativo: empieza en 500 km, expande hasta 800 km si hay < 6 EP
4. Cross-Validation Leave-One-Out sobre todas las EP del área para elegir (n, p) óptimos
5. IDW con la configuración elegida → interpola delta en el punto de interés
6. `coord_POSGAR07 = coord_IGS20 - delta`

Retorna `TransformResult` con:
- `lat`, `lon` (POSGAR07)
- `cv_error_cm` (vector), `cv_error_lat_cm`, `cv_error_lon_cm` (por componente)
- `n_used`, `p_used`, `n_ep_cv`, `wk_used`, `radius_km`, `ep_nearest`

**`dd2dms()`** vive en `transform.py` (migrada desde `itrf2posgar.py`).

### Datos (pickles)

- `ramsac`: dict `{ep: {lat, lon}}` — coordenadas POSGAR07 de 127 EP
  - **No tiene `alt`** — por eso la transformación altimétrica no está implementada
- `iws`: dict `{wk: {ep: {lat, lon, alt}}}` — soluciones semanales IGS20
  - 1007 semanas (GPS week 1388–2398 aprox.)
  - No todas las EP en todas las semanas (la red fue creciendo)
  - Algunas EP tienen períodos de inactividad
- `sws`: similar a `iws`, soluciones alternativas
- Los pickles se generan en otro servidor por procesamiento relativo y ajuste de red
- Se actualizan periódicamente (cada semana nueva de la red RAMSAC)

### Frontend (index.html)

- **Dos pestañas**: "PPP + Transformación" (con RINEX) y "Solo Transformación" (coordenadas directas)
- El JS construye el HTML del resultado con `buildResultHTML(data)` usando campos individuales
  del JSON — **no** usa HTML pre-renderizado del backend
- `buildCombinedQuality(d)`: calcula incertidumbre total = √(σ_PPP_real² + σ_CV²)
  donde σ_PPP_real = max(σ_formal, 1 cm)
- Glosario de términos geodésicos (PPP, EP, IDW, CV, POSGAR07, IGS20, ITRF, σ(95%), wk, n, p)
  con tooltips al hover sobre `?`
- Tile de mapa: CartoDB dark_all (nombres en español → "Islas Malvinas" correcto)
- Etiquetas en mapa: `base-label` (punto de interés, verde) y `ep-label` (EP usadas, azul)

### Calidad del resultado

Dos indicadores independientes (no se propagan uno en el otro):
- **σ(95%) NRCan**: incertidumbre formal del PPP, optimista por naturaleza (~0.5 cm formal ≠ ~1-3 cm real)
- **Error CV**: empírico y realista, mide exactitud espacial de la interpolación IDW
- **Combinación**: σ_total = √(σ_PPP_real² + σ_CV²), mostrado por componente y vectorial

### Pestaña "Solo Transformación"

- Formulario: lat, lon, hgt (opcional), fecha
- Endpoint: `POST /api/transform` — corre `transform_itrf_to_posgar07` directamente en web
  (sin Celery, es instantáneo)
- Asume `ref_frame = "IGS20"` (el usuario ingresa las coordenadas manualmente)

## Cómo correr localmente

```bash
# Primera vez:
cp .env.example .env
# Editar .env con CSRS_USER_EMAIL

docker compose up --build

# Uso normal:
docker compose up

# Tras editar código:
docker compose restart worker web

# Limpiar Redis (jobs y archivos RINEX temporales):
docker compose exec redis redis-cli FLUSHDB

# Ver logs:
docker compose logs -f worker

# Linting y formato (ruff, dentro del contenedor):
docker compose exec web ruff check app/ ppp/transform.py ppp/geodata.py
docker compose exec web ruff format app/ ppp/transform.py ppp/geodata.py
```

> Los archivos RINEX se transfieren vía Redis (clave `rinex:{job_id}`, TTL 1h).
> No se necesitan volúmenes compartidos entre web y worker.

URLs locales:
- App: http://localhost:8000
- Flower (monitor Celery): http://localhost:5555

## Deploy en Railway

Se necesitan 3 servicios: **Redis** (catálogo Railway) + **web** + **worker**.

- `web`: Start Command = `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- `worker`: Start Command = `celery -A app.tasks.celery_app worker --loglevel=info --concurrency=2`
- Ambos linkeados al mismo Redis

Variables de entorno requeridas:
```
REDIS_URL          → referencia al servicio Redis de Railway
CSRS_USER_EMAIL    → email registrado en NRCan
CSRS_GET_MAX       → 60 (10 minutos máximo de espera)
PPP_DIR            → /app/ppp
```

Los pickles NO están en git. Subir a Railway via CLI:
```bash
railway run --service web cp ramsac.pickle /app/ppp/ramsac.pickle
railway run --service web cp iws.pickle    /app/ppp/iws.pickle
railway run --service web cp sws.pickle    /app/ppp/sws.pickle
```
O configurar un volumen persistente montado en `/app/ppp` (recomendado para actualizaciones frecuentes).

## Trabajo pendiente / ideas futuras

- **Transformación altimétrica**: requiere obtener alturas elipsoidales POSGAR07
  de las EP desde IGN (scraping de ign.gob.ar o solicitud formal)
- **Persistencia con PostgreSQL**: tabla de resultados, historial de jobs
  (código preparado, descomentar en requirements.txt + agregar models.py)
- **Actualización automática de pickles**: script/cron que regenere iws.pickle
  semanalmente desde el servidor de procesamiento de la red RAMSAC
- **Comparación de métodos**: mostrar simultáneamente IDW CV-LOO vs calcv10 vs calcv15
  para validación y benchmarking

## Archivos que NO deben modificarse sin entender su contexto

- `ppp/itrf2posgar.py` y `ppp/calc.py`: scripts originales mantenidos como referencia.
  El pipeline activo usa `transform.py` en su lugar.
- `ppp/geodata.py`: usar rutas absolutas con `os.path.abspath(__file__)` — no rutas relativas.
