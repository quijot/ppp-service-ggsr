# Geodata RAMSAC — Obtención y actualización

## Fuentes de datos

- **RAMSAC**: coordenadas oficiales POSGAR07 de cada EP, descargadas desde la API de IGN-Ar.
- **Soluciones Diarias IGN-Ar (iws)**: soluciones diarias en marco IGS14/IGS20, descargadas vía FTP desde `ramsac.ign.gob.ar`. Se agrupan por semana GPS en la estructura `iws[week][ep] = {lat, lon, alt}`.

SIRGAS fue removido del pipeline activo (ver historial de git si se necesita recuperar).

## Actualización automática en Railway

En producción (Railway), la geodata se actualiza automáticamente:

- **Tarea Celery** `update_geodata` — corre todos los martes a las 3:00 UTC via Celery Beat (embebido en el worker).
- **Bootstrap automático** — si Redis no tiene datos al arrancar el worker (primer deploy o después de un flush), se lanza automáticamente una descarga completa desde semana GPS 1388. Tarda ~2 horas (descarga ~7000 archivos .crd vía FTP).
- **Almacenamiento**: los datos se guardan en Redis (compartido entre web y worker) y también como pickles en el volumen del worker (`PPP_DATA_DIR`, por defecto `/app/ppp/data`).

### Fallback en cascada

```
1. Redis (geodata:ramsac / geodata:iws)      ← fuente primaria, compartida
2. $PPP_DATA_DIR/ramsac.pickle + iws.pickle  ← fallback si Redis está vacío
3. FileNotFoundError con mensaje claro       ← si no hay ninguna fuente
```

Los archivos `.crd` descargados se cachean en `$PPP_DATA_DIR/iws_crd/` (~40 MB).
Las actualizaciones incrementales solo descargan semanas nuevas.

### Actualización manual (Railway)

Desde Railway shell (`railway shell --service worker`):

```bash
# Actualización incremental (solo semanas nuevas)
python -c "from app.tasks import update_geodata; update_geodata.apply(kwargs={'full': False})"

# Bootstrap completo (re-descarga todo)
python -c "from app.tasks import update_geodata; update_geodata.apply(kwargs={'full': True})"

# Verificar última semana en Redis
python -c "import redis, os; r = redis.from_url(os.environ['REDIS_URL']); print(r.get('geodata:last_week'))"
```

## Variables de entorno

```bash
IGN_FTP_USER=<usuario FTP IGN-Ar>
IGN_FTP_PASS=<contraseña FTP IGN-Ar>
PPP_DATA_DIR=/app/ppp/data   # Railway: montar volumen aquí (pickles + .crd)
```
