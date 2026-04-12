"""
geodata.py — Carga los archivos pickle de datos geodésicos RAMSAC.

Exporta:
  - ramsac : coordenadas de referencia de las estaciones RAMSAC
  - iws    : soluciones semanales IGS (indexado por GPS week)
  - sws    : soluciones semanales alternativas

Usa rutas absolutas para funcionar independientemente del working directory.
"""

import os
import pickle

_dir = os.path.dirname(os.path.abspath(__file__))


def _load(filename: str):
    with open(os.path.join(_dir, filename), "rb") as f:
        return pickle.load(f)


ramsac = _load("ramsac.pickle")
iws = _load("iws.pickle")
sws = _load("sws.pickle")
