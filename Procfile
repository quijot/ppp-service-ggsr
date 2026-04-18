# Procfile — usado por Railway para definir los procesos del servicio
#
# IMPORTANTE: Railway NO ejecuta múltiples procesos de un mismo Procfile
# en el mismo servicio. Para correr web + worker en Railway necesitás
# DOS servicios separados apuntando al mismo repo, con Start Commands distintos.
# Este Procfile es útil para referencia y para plataformas que sí lo soportan.
#
# Ver README → Deploy en Railway para instrucciones detalladas.

web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
worker: celery -A app.tasks.celery_app worker --loglevel=info --concurrency=2 --beat
