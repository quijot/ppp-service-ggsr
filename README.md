# Servicio PPP Experimental

**NRCan CSRS-PPP + Transformación ITRF → POSGAR07**

Pipeline:
1. El usuario sube un archivo RINEX
2. Se envía a NRCan CSRS-PPP via API
3. Se espera el resultado (polling, ~5-15 min)
4. Se parsea el `.sum` y se transforman las coords ITRF → POSGAR07
5. Se muestra el resultado con mapa interactivo

---

## Estructura

```
ppp-service/
├── app/
│   ├── main.py        # FastAPI: endpoints web
│   ├── tasks.py       # Celery: worker asincrónico
│   ├── parser.py      # Parser del .sum de NRCan
│   ├── config.py      # Settings via variables de entorno
│   └── templates/
│       └── index.html # Frontend
├── ppp/
│   ├── calc2.py       # Tu script (copiar aquí)
│   ├── itrf2posgar.py # Tu script (copiar aquí)
│   ├── geodata.py     # Placeholder — reemplazar con el real
│   ├── ramsac.pickle  # Copiar desde tu servidor
│   └── iws.pickle     # Copiar desde tu servidor
├── Dockerfile
├── docker-compose.yml
├── railway.toml
└── Procfile
```

---

## Setup local (desarrollo)

### 1. Archivos propios

Copiá en `ppp/`:
- `calc2.py` y `itrf2posgar.py` (tus scripts)
- `geodata.py` real (o dejá el placeholder si usás pickles)
- `ramsac.pickle` e `iws.pickle`
- Cualquier otro módulo que importe `itrf2posgar.py` (vms2017, vms2015, vms2009, sumBL, velar2015a2007)

### 2. Configuración

```bash
cp .env.example .env
# Editar .env con tu email de NRCan
```

### 3. Levantar con Docker Compose

```bash
docker compose up --build
```

Accedé a:
- **App**: http://localhost:8000
- **Flower** (monitor Celery): http://localhost:5555

### 4. Sin Docker (manual)

```bash
# Terminal 1: Redis
redis-server

# Terminal 2: Web
pip install -r requirements.txt
uvicorn app.main:app --reload

# Terminal 3: Worker
celery -A app.tasks.celery_app worker --loglevel=info
```

---

## Deploy en Railway

### Servicios necesarios en Railway:
1. **Redis** — Add Service → Redis (Railway lo provee)
2. **web** — conectá el repo, Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. **worker** — mismo repo, Start Command: `celery -A app.tasks.celery_app worker --loglevel=info`

### Variables de entorno (en cada servicio):
| Variable | Valor |
|---|---|
| `REDIS_URL` | Lo provee Railway automáticamente al linkear Redis |
| `CSRS_USER_EMAIL` | Tu email registrado en NRCan |
| `CSRS_GET_MAX` | `60` (10 min de espera máxima) |
| `PPP_DIR` | `/app/ppp` |

### Archivos `.pickle`
Los pickles son archivos binarios de datos. Railway no los excluye del build, pero:
- **No los subas a git** (están en `.gitignore`)
- Usá el **Railway CLI** para subirlos al filesystem del servicio, o
- Configurá un **volumen persistente** en Railway y subilos ahí

```bash
# Con Railway CLI
railway run -- bash -c "ls /app/ppp"
```

---

## Notas técnicas

- **Tiempo de procesamiento**: NRCan puede tardar entre 2 y 30 minutos. El frontend hace polling cada 5 segundos.
- **Límite de archivo**: 20 MB (igual que NRCan).
- **Archivos temporales**: Se limpian automáticamente al finalizar cada job.
- **PostgreSQL**: El código tiene preparada la variable `DATABASE_URL`. Para activar persistencia de resultados, descomentar las líneas en `requirements.txt` y agregar un modelo SQLAlchemy.

---

## Agregar PostgreSQL (futuro)

1. Descomentar en `requirements.txt`: `asyncpg` y `sqlalchemy[asyncio]`
2. Agregar en Railway: **Add Service → PostgreSQL**
3. Railway inyecta `DATABASE_URL` automáticamente
4. Crear un `models.py` con la tabla de resultados y linkear desde `tasks.py`
