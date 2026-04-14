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
| **Redis** | Catálogo Railway | Broker y backend de Celery |
| **web** | GitHub repo | FastAPI (Uvicorn) |
| **worker** | GitHub repo | Celery worker |

### Procedimiento

**1. Crear el proyecto en Railway**
- New Project → Deploy from GitHub repo → seleccionar `ppp-service-ggsr`

**2. Agregar Redis**
- En el proyecto: Add Service → Redis
- Railway inyecta `REDIS_URL` automáticamente a los servicios que lo linken

**3. Configurar el servicio `web`**
- Railway detecta el `Dockerfile` y `railway.toml` automáticamente
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Linkear Redis (Variables → Add Reference → REDIS_URL)
- Variables de entorno:

| Variable | Valor |
|---|---|
| `REDIS_URL` | referencia al servicio Redis |
| `CSRS_USER_EMAIL` | tu email registrado en NRCan |
| `CSRS_GET_MAX` | `60` (máx 10 min de espera por respuesta de NRCan) |
| `PPP_DIR` | `/app/ppp` |

**4. Configurar el servicio `worker`**
- Add Service → GitHub Repo → mismo repo
- Start Command: `celery -A app.tasks.celery_app worker --loglevel=info --concurrency=2`
- Mismas variables de entorno que `web`
- **No** necesita puerto expuesto

**5. Subir los archivos `.pickle`**

Los pickles no están en git. Hay dos opciones:

**Opción A — Railway CLI** (recomendada para pickles pequeños):
```bash
# Instalar Railway CLI
npm install -g @railway/cli
railway login

# Copiar pickles al servicio web
railway run --service web cp /ruta/local/ramsac.pickle /app/ppp/ramsac.pickle
railway run --service web cp /ruta/local/iws.pickle    /app/ppp/iws.pickle
railway run --service web cp /ruta/local/sws.pickle    /app/ppp/sws.pickle
```

**Opción B — Volumen persistente** (recomendada para actualización frecuente):
- En Railway: configurar un volumen persistente montado en `/app/ppp`
- Subir los pickles al volumen vía SSH o CLI
- El volumen persiste entre deploys

> **Nota**: los pickles se actualizan periódicamente (nuevas semanas de la red RAMSAC).
> Con la Opción B podés actualizarlos sin necesidad de redeploy.

---

## Variables de entorno — referencia completa

| Variable | Default | Descripción |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | URL de Redis |
| `CSRS_USER_EMAIL` | `ppp@municipalidad.gob.ar` | Email registrado en NRCan |
| `CSRS_GET_MAX` | `60` | Intentos de polling (× 10s = tiempo máx. de espera) |
| `CSRS_MODE` | `Static` | Modo de procesamiento PPP |
| `CSRS_REF` | `ITRF` | Marco de referencia solicitado a NRCan |
| `PPP_DIR` | `/app/ppp` | Path al directorio de módulos geodésicos |
| `UPLOAD_DIR` | `/tmp/ppp_uploads` | Path para archivos RINEX temporales |
| `RESULTS_DIR` | `/tmp/ppp_results` | Path para resultados temporales de NRCan |
| `DATABASE_URL` | *(vacío)* | PostgreSQL — para activar persistencia de resultados |

---

## Notas técnicas

- **Tiempo de procesamiento**: NRCan puede tardar entre 2 y 30 minutos según carga del servidor y duración del RINEX. El frontend hace polling cada 5 segundos.
- **Límite de archivo**: 20 MB (igual que NRCan).
- **Marco de referencia**: NRCan v5 usa IGS20 para datos recientes y posiblemente IGS14 para históricos pre-2022. El servicio lee el marco del `.sum` y lo muestra explícitamente.
- **Altura elipsoidal**: se reporta en el marco de NRCan (IGS20) sin transformar, ya que aún no se pueden obtener sistemáticamente desde `ramsac` las alturas POSGAR07 de referencia para calcular el delta altimétrico.
- **Archivos temporales**: se limpian automáticamente al finalizar cada job.

## Activar persistencia con PostgreSQL (futuro)

1. Descomentar en `requirements.txt`: `asyncpg` y `sqlalchemy[asyncio]`
2. En Railway: **Add Service → PostgreSQL**
3. Railway inyecta `DATABASE_URL` automáticamente
4. Agregar `models.py` con la tabla de resultados y linkear desde `tasks.py`
