# Servicio PPP Experimental — GGSR

**Grupo de Geodesia Satelital de Rosario**  
NRCan CSRS-PPP + Transformación ITRF → POSGAR07

El servicio procesa archivos RINEX a través de la API de NRCan CSRS-PPP y transforma
las coordenadas resultantes del marco ITRF/IGS al marco oficial argentino POSGAR07
(ITRF2005, época 2006.632), usando interpolación IDW sobre la red RAMSAC con
selección automática de parámetros por Cross-Validation Leave-One-Out.

---

## Estructura del proyecto

```
ppp-service-ggsr/
├── app/
│   ├── main.py           # FastAPI: endpoints web y API
│   ├── tasks.py          # Celery: worker asincrónico (pipeline completo)
│   ├── parser.py         # Parser del .sum de NRCan v5.x
│   ├── config.py         # Settings via variables de entorno (pydantic-settings)
│   └── templates/
│       ├── index.html        # Frontend principal (dos pestañas: PPP y transformación directa)
│       └── como_funciona.html  # Documentación técnica del proceso
├── ppp/
│   ├── transform.py      # Módulo principal: transformación IGS20→POSGAR07 con CV-LOO
│   ├── geodata.py        # Carga ramsac.pickle, iws.pickle, sws.pickle
│   ├── calc.py           # Script original de referencia
│   ├── itrf2posgar.py    # Módulo original de referencia
│   ├── ramsac.pickle     # ⚠ NO en git — coordenadas POSGAR07 de la red RAMSAC
│   ├── iws.pickle        # ⚠ NO en git — soluciones semanales IGS20 de las EP
│   └── sws.pickle        # ⚠ NO en git — soluciones semanales alternativas
├── .env.example          # Plantilla de variables de entorno
├── .gitignore
├── Dockerfile
├── Procfile
├── README.md
├── docker-compose.yml    # Entorno de desarrollo local
├── railway.toml          # Configuración de deploy en Railway
└── requirements.txt
```

---

## Setup local (desarrollo)

### 1. Prerequisitos

- Docker y Docker Compose
- Los archivos `ramsac.pickle`, `iws.pickle` y `sws.pickle` en `ppp/`
- _Opcionalmente:_ Los módulos Python propios en `ppp/`: `calc.py`, `itrf2posgar.py`, y los grids de velocidad (`vms2017.py`, `vms2015.py`, `vms2009.py`, `sumBL.py`, `velar2015a2007.py`)

### 2. Configuración

```bash
cp .env.example .env
# Editar .env: al menos CSRS_USER_EMAIL con tu email registrado en NRCan
```

### 3. Levantar el entorno

```bash
# Primera vez o tras cambios en requirements.txt:
docker compose up --build

# Arranque normal:
docker compose up
```

Accedé a:
- **App**: http://localhost:8000
- **Flower** (monitor de tareas Celery): http://localhost:5555

### 4. Comandos útiles

```bash
# Tras editar código Python (sin rebuild):
docker compose restart worker web

# Limpiar resultados viejos en Redis:
docker compose exec redis redis-cli FLUSHDB

# Ver logs del worker:
docker compose logs -f worker

# Entrar al contenedor del worker:
docker compose exec worker bash
```

---

## Deploy en Railway

Railway requiere **tres servicios** para este proyecto:

### Servicios necesarios

| Servicio | Tipo | Descripción |
|---|---|---|
| **Redis** | Catálogo Railway | Broker y backend de Celery + geodata compartida |
| **web** | GitHub repo | FastAPI (Uvicorn) — usa `railway.web.toml` |
| **worker** | GitHub repo | Celery worker + Beat — usa `railway.worker.toml` |

Cada servicio apunta al mismo repo. En Settings → Railway Config File, configurar:
- `web` → `railway.web.toml`
- `worker` → `railway.worker.toml`

### Procedimiento

**1. Crear el proyecto en Railway**
- New Project → Deploy from GitHub repo → seleccionar el repo

**2. Agregar Redis**
- Add Service → Redis
- Railway inyecta `REDIS_URL` automáticamente a los servicios que lo linken

**3. Configurar el servicio `web`**
- Variables de entorno:

| Variable | Valor |
|---|---|
| `REDIS_URL` | referencia al servicio Redis |
| `CSRS_USER_EMAIL` | email registrado en NRCan |
| `CSRS_GET_MAX` | `60` |
| `PPP_DIR` | `/app/ppp` |

**4. Configurar el servicio `worker`**
- Mismas variables que `web`, más:

| Variable | Valor |
|---|---|
| `PPP_DATA_DIR` | `/app/ppp/data` |
| `IGN_FTP_USER` | usuario FTP IGN-Ar |
| `IGN_FTP_PASS` | contraseña FTP IGN-Ar |

**5. Crear el volumen persistente en el worker** ⚠️ paso manual

El volumen no se puede automatizar via `railway.toml`. Hacerlo desde el dashboard:

> Worker service → Storage → Add Volume → Mount Path: `/app/ppp/data`

El volumen persiste entre deploys y almacena los pickles y caché de archivos `.crd` (~40 MB).
Sin el volumen el sistema igual funciona, pero el bootstrap se relanza completo en cada deploy (~2 hs).

**6. Primer deploy**

Al arrancar por primera vez (o si Redis está vacío), el worker lanza automáticamente
`update_geodata(full=True)` via la señal `worker_ready`. Descarga RAMSAC + soluciones
IGN-Ar desde la semana 1388 (~2 hs). Durante ese tiempo los jobs PPP fallan con un mensaje claro. Las actualizaciones incrementales posteriores corren cada martes 3:00 UTC via Celery Beat.

**Verificar estado desde Railway shell** (`railway shell --service worker`):
```bash
# Ver última semana descargada
python -c "import redis, os; r = redis.from_url(os.environ['REDIS_URL']); print(r.get('geodata:last_week'))"

# Lanzar actualización incremental manualmente
python -c "from app.tasks import update_geodata; update_geodata.apply(kwargs={'full': False})"
```

---

## Variables de entorno — referencia completa

| Variable | Default | Descripción |
|---|---|---|
| `REDIS_URL` | inyectado por Railway / `redis://localhost:6379/0` en local | URL de Redis |
| `CSRS_USER_EMAIL` | — | Email registrado en NRCan |
| `CSRS_GET_MAX` | `60` | Intentos de polling (× 10s = tiempo máx. de espera) |
| `CSRS_MODE` | `Static` | Modo de procesamiento PPP |
| `CSRS_REF` | `ITRF` | Marco de referencia solicitado a NRCan |
| `PPP_DIR` | `/app/ppp` | Path a los módulos Python geodésicos — **nunca montar un volumen aquí** |
| `PPP_DATA_DIR` | *(obligatorio en Railway)* | Path para pickles y caché .crd — montar el volumen aquí (`/app/ppp/data`). Si no se setea, cae a `PPP_DIR`, lo que oculta los módulos si hay un volumen montado. |
| `IGN_FTP_USER` | — | Usuario FTP IGN-Ar (solo worker) |
| `IGN_FTP_PASS` | — | Contraseña FTP IGN-Ar (solo worker) |
| `RESULTS_DIR` | `/tmp/ppp_results` | Path para resultados temporales de NRCan (worker) |
| `DATABASE_URL` | *(vacío)* | PostgreSQL — para activar persistencia de resultados |

---

## Documentación técnica

- [Geodata RAMSAC — pipeline de obtención y actualización](docs/geodata.md)

## Notas técnicas

- **Tiempo de procesamiento**: NRCan puede tardar entre 2 y 30 minutos según carga del servidor y duración del RINEX. El frontend hace polling cada 5 segundos.
- **Límite de archivo**: 20 MB (igual que NRCan).
- **Marco de referencia**: NRCan v5 usa IGS20 para datos recientes y posiblemente IGS14 para históricos pre-2022. El servicio lee el marco del `.sum` y lo muestra explícitamente.
- **Altura elipsoidal**: se reporta en el marco de NRCan (IGS20) sin transformar, ya que aún no se pueden obtener sistemáticamente desde `ramsac` las alturas POSGAR07 de referencia para calcular el delta altimétrico.
- **Transferencia web→worker**: el archivo RINEX se almacena temporalmente en Redis (TTL 1 hora) y se recupera por el worker al inicio de la tarea. No se requieren volúmenes compartidos entre servicios.
- **Archivos temporales**: se limpian automáticamente al finalizar cada job.

## Activar persistencia con PostgreSQL (futuro)

1. Descomentar en `requirements.txt`: `asyncpg` y `sqlalchemy[asyncio]`
2. En Railway: **Add Service → PostgreSQL**
3. Railway inyecta `DATABASE_URL` automáticamente
4. Agregar `models.py` con la tabla de resultados y linkear desde `tasks.py`
