"""
transform.py
============
Transformación de coordenadas ITRF → POSGAR07 mediante
interpolación IDW sobre la red RAMSAC.

FUNDAMENTO DEL MÉTODO
---------------------
POSGAR07 es ITRF2005/IGS05 fijado a la época 2006.632. Las coordenadas que
devuelve el servicio PPP de NRCan están en ITRFx/IGSx (según la época)
referidas a la época de las observaciones RINEX.

La diferencia entre ambos marcos, para un punto dado en una época dada,
combina varios efectos físicos:
  - Deriva continental (movimiento de la placa Sudamericana, ~2-3 cm/año)
  - Diferencia de época (ITRFx/IGSx en época de observación vs POSGAR07 en 2006.632)
  - Cambios de realización entre marcos (ITRFx/IGSx vs ITRF2005)
  - Movimientos sísmicos locales
  - Efectos locales de cada estación

En lugar de modelar cada efecto por separado (lo cual requiere modelos de
velocidades, modelos sísmicos, etc.), este módulo los cuantifica
empíricamente a partir de las Estaciones Permanentes de RAMSAC:

  delta(EP, wk) = iws[wk][EP] - ramsac[EP]

donde:
  - iws[wk][EP]  : coordenadas ITRFx/IGSx de la EP para la semana GPS `wk`,
                   calculadas por procesamiento relativo y ajuste de red
  - ramsac[EP]   : coordenadas oficiales POSGAR07 de la EP

Este vector `delta` absorbe todos los efectos mencionados de forma conjunta.
Luego se interpola espacialmente hacia el punto de interés usando IDW.

ALGORITMO
---------
1. Para la semana GPS más cercana a las observaciones, reunir todos los
   deltas disponibles de EP dentro de un radio de búsqueda.

2. Filtrar EP con deltas anómalos (outliers estadísticos) que podrían
   introducir error en la interpolación.

3. Elegir los parámetros IDW (n, p) mediante Cross-Validation Leave-One-Out
   sobre el conjunto de EP disponibles: se elige la configuración que
   mejor predice los deltas conocidos en la región de interés.

4. Aplicar IDW con la configuración elegida para interpolar el delta
   en el punto de interés y aplicarlo a las coordenadas PPP.

5. Devolver las coordenadas transformadas junto con una estimación de
   calidad (error CV) que permite al usuario conocer la confianza del
   resultado.

DIFERENCIA CON EL ALGORITMO ANTERIOR (get_best_configuration)
--------------------------------------------------------------
El algoritmo anterior evaluaba la configuración IDW sobre una sola EP
(la más cercana al punto de interés), lo cual tiene dos problemas:
  a) Un solo punto de evaluación es muy ruidoso.
  b) La EP más cercana puede estar en un contexto geodésico diferente
     al punto de interés, haciendo que la configuración óptima para
     esa EP no sea la óptima para interpolar en otra ubicación.

Este módulo evalúa la configuración sobre TODAS las EP del área,
haciendo la selección mucho más robusta y justificable.
"""

import statistics
from collections import namedtuple
from math import trunc

from geographiclib.geodesic import Geodesic


# ---------------------------------------------------------------------------
# Tipos de resultado
# ---------------------------------------------------------------------------

TransformResult = namedtuple(
    "TransformResult",
    [
        "lat",  # latitud POSGAR07 transformada (grados decimales)
        "lon",  # longitud POSGAR07 transformada (grados decimales)
        "cv_error_cm",  # error estimado por Cross-Validation (cm), indicador de calidad
        "cv_error_lat_cm",  # error CV componente latitud (cm)
        "cv_error_lon_cm",  # error CV componente longitud (cm)
        "n_ep_used",  # cantidad de EP usadas en la interpolación final
        "n_ep_cv",  # cantidad de EP usadas en el CV (base estadística)
        "n_used",  # parámetro n elegido para IDW
        "p_used",  # parámetro p elegido para IDW
        "wk_used",  # semana GPS efectivamente usada
        "ep_nearest",  # dict {nombre_EP: distancia_km} de las EP usadas
        "radius_km",  # radio de búsqueda usado (puede ser expandido)
    ],
)


# ---------------------------------------------------------------------------
# Parámetros del algoritmo (ajustables)
# ---------------------------------------------------------------------------

# Configuraciones IDW (n, p) a evaluar en el CV.
# n = cantidad de EP a usar en la interpolación
# p = exponente de la distancia en la ponderación (mayor p = más peso a EP cercanas)
CONFIGS_TO_TEST = [
    (2, 1),
    (2, 2),
    (2, 3),
    (3, 1),
    (3, 2),
    (3, 3),
    (5, 1),
    (5, 2),
    (5, 3),
    (7, 1),
    (7, 2),
    (7, 3),
    (10, 1),
    (10, 2),
    (10, 3),
]

# Radio inicial de búsqueda de EP (km).
# Con 500 km hay típicamente 15-35 EP en Argentina central.
# En zonas despobladas (Patagonia) puede ser necesario expandirlo.
RADIUS_INITIAL_KM = 500

# Radio máximo de búsqueda (km). Si no hay suficientes EP en el radio
# inicial, se expande hasta este límite.
RADIUS_MAX_KM = 800

# Cantidad mínima de EP para que el CV tenga base estadística suficiente.
# Con menos EP que esto, el CV puede elegir una configuración por azar.
MIN_EP_FOR_CV = 6

# Cantidad mínima de EP para poder interpolar (sin CV).
# Si hay menos que esto, no se puede hacer una interpolación confiable.
MIN_EP_FOR_INTERPOLATION = 3

# Umbral de outlier: si el delta de una EP difiere más de este factor
# de la desviación estándar robusta del conjunto, se descarta.
# Se usa MAD (Median Absolute Deviation) en lugar de std para robustez.
OUTLIER_THRESHOLD_MAD = 3.5

# Configuración fallback si no hay suficientes EP para CV.
FALLBACK_N = 3
FALLBACK_P = 1


# ---------------------------------------------------------------------------
# Funciones geométricas
# ---------------------------------------------------------------------------


def _dist_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia geodésica entre dos puntos (km)."""
    return Geodesic.WGS84.Inverse(lat1, lon1, lat2, lon2)["s12"] / 1000.0


def _arc_lat_cm_per_sec(lat: float) -> float:
    """
    Metros por segundo de arco en dirección Norte (arco de meridiano).
    Se multiplica por 100 para obtener cm/segundo.
    Depende de la latitud por el achatamiento del elipsoide.
    """
    return _dist_km(lat, 0.0, lat + 1 / 3600, 0.0) * 100_000.0


def _arc_lon_cm_per_sec(lat: float) -> float:
    """
    Metros por segundo de arco en dirección Este (arco de paralelo).
    Se multiplica por 100 para obtener cm/segundo.
    Varía con el coseno de la latitud.
    """
    return _dist_km(lat, 0.0, lat, 1 / 3600) * 100_000.0


# ---------------------------------------------------------------------------
# Utilidades de formato
# ---------------------------------------------------------------------------


def dd2dms(coord: float) -> str:
    """
    Convierte grados decimales a string 'GG MM SS.SSSSS'.
    Ejemplo: -25.573957 → '-25 34 26.24520'
    """
    dd = trunc(coord)
    mm_ = (coord - dd) * 60
    mm = trunc(mm_)
    ss = (mm_ - mm) * 60
    return "{} {:02d} {:.5f}".format(dd, abs(mm), round(abs(ss), 5))


# ---------------------------------------------------------------------------
# Carga y preparación de datos
# ---------------------------------------------------------------------------


def _load_candidates(
    center_lat: float,
    center_lon: float,
    wk: int,
    radius_km: float,
    iws: dict,
    ramsac: dict,
) -> dict:
    """
    Carga todas las EP disponibles en `iws[wk]` que están en `ramsac`
    y dentro de `radius_km` del punto central.

    Devuelve un dict:
      {
        "EP_NAME": {
          "lat": float,          # latitud POSGAR07 (ramsac)
          "lon": float,          # longitud POSGAR07 (ramsac)
          "dlat_cm": float,      # delta latitud en cm (iws - ramsac)
          "dlon_cm": float,      # delta longitud en cm (iws - ramsac)
          "dist_km": float,      # distancia al punto central
        },
        ...
      }

    La conversión de segundos de arco a cm es necesaria porque IDW
    trabaja en unidades métricas para que n y p tengan interpretación
    geométrica consistente. Si trabajáramos en segundos de arco, las
    distancias latitudinales y longitudinales no serían comparables
    entre sí (por el achatamiento del elipsoide y la convergencia de
    meridianos).
    """
    eps_wk = iws.get(wk, {})
    candidates = {}

    for ep, ref_coord in ramsac.items():
        # La EP debe tener datos en esta semana
        if ep not in eps_wk:
            continue

        dist = _dist_km(center_lat, center_lon, ref_coord["lat"], ref_coord["lon"])
        if dist > radius_km:
            continue

        lat_ref = ref_coord["lat"]
        am = _arc_lat_cm_per_sec(lat_ref)  # cm por segundo de arco en latitud
        ap = _arc_lon_cm_per_sec(lat_ref)  # cm por segundo de arco en longitud

        # Delta en segundos de arco, luego convertido a cm
        dlat_sec = (eps_wk[ep]["lat"] - ref_coord["lat"]) * 3600
        dlon_sec = (eps_wk[ep]["lon"] - ref_coord["lon"]) * 3600

        candidates[ep] = {
            "lat": ref_coord["lat"],
            "lon": ref_coord["lon"],
            "dlat_cm": dlat_sec * am,
            "dlon_cm": dlon_sec * ap,
            "dist_km": dist,
        }

    return candidates


def _filter_outliers(candidates: dict) -> dict:
    """
    Elimina EP con deltas anómalos usando MAD (Median Absolute Deviation).

    MAD es más robusto que la desviación estándar porque no es afectada
    por los propios outliers que intenta detectar. Por ejemplo, CAEP
    tuvo un delta de -12 cm en lon en la semana 2388, cuando el valor
    habitual es -2/-3 cm. Con std ese outlier inflaría el umbral de
    detección; con MAD no.

    Se filtra por componente (lat y lon) por separado, ya que una EP
    puede ser confiable en una componente y anómala en otra.

    Un punto se considera outlier si:
      |delta - mediana| > OUTLIER_THRESHOLD_MAD × MAD

    donde MAD = mediana(|delta_i - mediana(delta)|)
    """
    if len(candidates) < 4:
        # Con menos de 4 EP no hay base para detectar outliers
        return candidates

    dlats = [v["dlat_cm"] for v in candidates.values()]
    dlons = [v["dlon_cm"] for v in candidates.values()]

    def mad_filter(values: list, threshold: float) -> list:
        """Devuelve máscara booleana: True = inlier, False = outlier."""
        med = statistics.median(values)
        abs_devs = [abs(v - med) for v in values]
        mad = statistics.median(abs_devs)
        if mad == 0:
            # Todos los valores son idénticos → ningún outlier
            return [True] * len(values)
        return [abs(v - med) / mad <= threshold for v in values]

    mask_lat = mad_filter(dlats, OUTLIER_THRESHOLD_MAD)
    mask_lon = mad_filter(dlons, OUTLIER_THRESHOLD_MAD)

    return {
        ep: data
        for (ep, data), ok_lat, ok_lon in zip(candidates.items(), mask_lat, mask_lon)
        if ok_lat and ok_lon
    }


# ---------------------------------------------------------------------------
# IDW
# ---------------------------------------------------------------------------


def _idw(ep_data: list, n: int, p: int) -> tuple[float, float]:
    """
    Interpolación IDW sobre una lista de EP.

    Args:
        ep_data: lista de (dlat_cm, dlon_cm, dist_km) de las EP disponibles
        n:       cantidad de EP más cercanas a usar
        p:       exponente de la ponderación por distancia

    La ponderación w_i = 1 / dist_i^p hace que las EP más cercanas
    tengan más influencia. Con p=1 la influencia decae linealmente;
    con p=2 decae cuadráticamente (más localizado).

    Se evita división por cero reemplazando distancias nulas por 1e-6 km
    (equivale a que el punto coincide exactamente con una EP).
    """
    # Tomar las n EP más cercanas
    ep_sorted = sorted(ep_data, key=lambda x: x[2])[:n]

    weights = [1.0 / max(x[2], 1e-6) ** p for x in ep_sorted]
    W = sum(weights)

    dlat = sum(w * x[0] for w, x in zip(weights, ep_sorted)) / W
    dlon = sum(w * x[1] for w, x in zip(weights, ep_sorted)) / W

    return dlat, dlon


# ---------------------------------------------------------------------------
# Cross-Validation Leave-One-Out
# ---------------------------------------------------------------------------


def _cv_loo_error(
    candidates: dict, n: int, p: int
) -> tuple[float, float, float] | None:
    """
    Calcula el error medio de Cross-Validation Leave-One-Out para una
    configuración (n, p) dada.

    Procedimiento:
      Para cada EP del conjunto:
        1. Removerla temporalmente ("dejarla fuera")
        2. Interpolar su delta usando las EP restantes con IDW(n, p)
        3. Comparar el delta interpolado con el delta real
      Error CV = promedio de los errores individuales (en cm)

    Esto mide qué tan bien la configuración (n, p) predice el campo
    vectorial en puntos que no fueron usados en la interpolación, que
    es exactamente lo que queremos hacer con el punto de interés.

    Requiere al menos n+1 EP (necesitamos n EP para interpolar y 1
    para evaluar en cada paso).

    Retorna (error_vector_cm, error_lat_cm, error_lon_cm) o None si no hay
    suficientes EP.

    El error por componente permite:
      - Entender si la interpolación es peor en lat o en lon
      - Detectar zonas con gradientes asimétricos (ej: cordillera afecta
        más a lon que a lat)
      - Dar incertidumbre por componente en el output final

    Devuelve None si no hay suficientes EP.
    """
    if len(candidates) < n + 1:
        return None

    errors_vec = []
    errors_lat = []
    errors_lon = []

    for ep_test, test_data in candidates.items():
        # Coordenadas de la EP que dejamos fuera
        test_lat = test_data["lat"]
        test_lon = test_data["lon"]

        # Construir el conjunto de EP restantes con sus distancias
        # desde el punto de evaluación (ep_test), no desde el punto
        # de interés original.
        #
        # ¿Por qué desde ep_test y no desde el punto de interés?
        # Porque estamos simulando lo que IDW haría si tuviera que
        # predecir el delta en ep_test. La distancia relevante para
        # la ponderación es la distancia desde ep_test a cada EP
        # de apoyo, que es exactamente lo que IDW usaría en ese caso.
        ep_data = [
            (
                data["dlat_cm"],
                data["dlon_cm"],
                _dist_km(test_lat, test_lon, data["lat"], data["lon"]),
            )
            for ep, data in candidates.items()
            if ep != ep_test
        ]

        if len(ep_data) < n:
            # No hay suficientes EP restantes para esta configuración
            continue

        dlat_pred, dlon_pred = _idw(ep_data, n, p)

        err_lat = dlat_pred - test_data["dlat_cm"]
        err_lon = dlon_pred - test_data["dlon_cm"]
        err_vec = (err_lat**2 + err_lon**2) ** 0.5

        errors_vec.append(err_vec)
        errors_lat.append(abs(err_lat))
        errors_lon.append(abs(err_lon))

    if not errors_vec:
        return None

    return (
        statistics.mean(errors_vec),
        statistics.mean(errors_lat),
        statistics.mean(errors_lon),
    )


def _choose_best_config(candidates: dict) -> tuple[int, int, float]:
    """
    Evalúa todas las configuraciones en CONFIGS_TO_TEST mediante CV-LOO
    y devuelve (n, p, cv_error_vec, cv_error_lat, cv_error_lon) con el menor
    error.

    Si ninguna configuración puede evaluarse (muy pocas EP),
    devuelve la configuración fallback.
    """
    best_n, best_p, best_err, best_err_lat, best_err_lon = (FALLBACK_N, FALLBACK_P, float("inf"), -1.0, -1.0)

    for n, p in CONFIGS_TO_TEST:
        err, err_lat, err_lon = _cv_loo_error(candidates, n, p)
        if err is not None and err < best_err:
            best_n, best_p, best_err, best_err_lat, best_err_lon = n, p, err, err_lat, err_lon

    if best_err == float("inf"):
        # No se pudo evaluar ninguna configuración
        # Devuelve fallback
        return (FALLBACK_N, FALLBACK_P, -1.0, -1.0, -1.0)

    return best_n, best_p, best_err, best_err_lat, best_err_lon


# ---------------------------------------------------------------------------
# Selección de semana GPS
# ---------------------------------------------------------------------------


def _find_best_week(target_wk: int, iws: dict) -> int:
    """
    Encuentra la semana GPS disponible en iws más cercana a target_wk.

    El servicio de NRCan devuelve coordenadas referidas a la época de
    las observaciones. Queremos usar la semana más cercana a esa época
    para minimizar la extrapolación temporal del campo de deltas.

    Busca primero hacia atrás (semanas pasadas con datos consolidados)
    y luego hacia adelante, prefiriendo semanas pasadas en caso de empate.
    """
    if target_wk in iws:
        return target_wk

    for offset in range(1, 200):
        if target_wk - offset in iws:
            return target_wk - offset
        if target_wk + offset in iws:
            return target_wk + offset

    raise ValueError(f"No se encontró ninguna semana disponible cerca de {target_wk}")


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------


def transform_itrf_to_posgar07(
    lat: float,
    lon: float,
    obs_wk: int,
    iws: dict,
    ramsac: dict,
    radius_km: float = RADIUS_INITIAL_KM,
    max_radius_km: float = RADIUS_MAX_KM,
) -> TransformResult:
    """
    Transforma coordenadas de ITRFx/IGSx (época de observación) a POSGAR07
    (ITRF2005, época 2006.632) usando interpolación IDW sobre la red RAMSAC.

    Args:
        lat:          Latitud PPP en ITRFx/IGSx (grados decimales, Sur negativo)
        lon:          Longitud PPP en ITRFx/IGSx (grados decimales, Oeste negativo)
        obs_wk:       Semana GPS de las observaciones RINEX
        iws:          Dict de soluciones semanales RAMSAC {wk: {ep: {lat, lon}}}
        ramsac:       Dict de coordenadas POSGAR07 de EP {ep: {lat, lon}}
        radius_km:    Radio inicial de búsqueda de EP (km)
        max_radius_km: Radio máximo si no hay suficientes EP en el radio inicial

    Returns:
        TransformResult con las coordenadas transformadas y métricas de calidad.

    Raises:
        RuntimeError si no hay suficientes EP para interpolar en ningún radio.
    """

    # ------------------------------------------------------------------
    # 1. Encontrar la semana más cercana con datos disponibles
    # ------------------------------------------------------------------
    wk = _find_best_week(obs_wk, iws)

    # ------------------------------------------------------------------
    # 2. Cargar EP candidatas, expandiendo el radio si es necesario
    #
    # La expansión es importante en zonas con baja densidad de EP
    # (Patagonia sur, zonas cordilleranas remotas). Sin expansión,
    # Comodoro Rivadavia por ejemplo solo tiene 2 EP en 300 km.
    # ------------------------------------------------------------------
    current_radius = radius_km
    candidates = {}

    while current_radius <= max_radius_km:
        candidates = _load_candidates(lat, lon, wk, current_radius, iws, ramsac)

        if len(candidates) >= MIN_EP_FOR_CV:
            break  # Suficientes EP para hacer CV con base estadística

        if current_radius >= max_radius_km:
            break  # Llegamos al límite, usamos lo que hay

        # Expandir radio en pasos de 100 km
        current_radius = min(current_radius + 100, max_radius_km)

    # ------------------------------------------------------------------
    # 3. Filtrar outliers
    #
    # Se hace DESPUÉS de la expansión de radio para tener más contexto
    # estadístico al calcular la MAD. Con pocas EP el filtro es más
    # conservador (ver _filter_outliers).
    # ------------------------------------------------------------------
    candidates = _filter_outliers(candidates)

    if len(candidates) < MIN_EP_FOR_INTERPOLATION:
        raise RuntimeError(
            f"Solo {len(candidates)} EP disponibles en {current_radius:.0f} km. "
            f"Mínimo requerido: {MIN_EP_FOR_INTERPOLATION}."
        )

    # ------------------------------------------------------------------
    # 4. Elegir configuración IDW óptima por Cross-Validation LOO
    # ------------------------------------------------------------------
    if len(candidates) >= MIN_EP_FOR_CV:
        best_n, best_p, cv_err, cv_lat, cv_lon = _choose_best_config(candidates)
    else:
        # Pocas EP: usar fallback sin CV
        # El error CV se marca como -1 para indicar que no fue calculado
        best_n, best_p, cv_error = FALLBACK_N, FALLBACK_P, -1.0

    # ------------------------------------------------------------------
    # 5. Interpolar el delta en el punto de interés
    #
    # Construir ep_data con distancias desde el punto de interés
    # (no desde ep_test como en el CV). Aquí la distancia relevante
    # para la ponderación IDW es la distancia real entre el punto
    # de interés y cada EP de apoyo.
    # ------------------------------------------------------------------
    ep_data = [
        (data["dlat_cm"], data["dlon_cm"], _dist_km(lat, lon, data["lat"], data["lon"]))
        for data in candidates.values()
    ]

    dlat_cm, dlon_cm = _idw(ep_data, best_n, best_p)

    # ------------------------------------------------------------------
    # 6. Aplicar el delta interpolado a las coordenadas PPP
    #
    # El delta está en cm. Convertir a grados:
    #   Δlat [°] = Δlat [cm] / (am [cm/seg] × 3600 [seg/°])
    #   Δlon [°] = Δlon [cm] / (ap [cm/seg] × 3600 [seg/°])
    #
    # Se resta el delta porque la transformación va de ITRFx/IGSx a POSGAR07:
    #   POSGAR07 = ITRFx/IGSx - (ITRFx/IGSx - POSGAR07) = ITRFx/IGSx - delta
    #
    # El delta representa cuánto difiere ITRFx/IGSx de POSGAR07, por lo tanto
    # restarlo lleva las coordenadas al marco POSGAR07.
    # ------------------------------------------------------------------
    am = _arc_lat_cm_per_sec(lat)
    ap = _arc_lon_cm_per_sec(lat)

    lat_posgar = lat - dlat_cm / (am * 3600)
    lon_posgar = lon - dlon_cm / (ap * 3600)

    # ------------------------------------------------------------------
    # 7. Construir reporte de EP usadas (las n más cercanas)
    # ------------------------------------------------------------------
    ep_sorted = sorted(candidates.items(), key=lambda x: x[1]["dist_km"])
    ep_nearest = {ep: data["dist_km"] for ep, data in ep_sorted[:best_n]}

    return TransformResult(
        lat=lat_posgar,
        lon=lon_posgar,
        cv_error_cm=cv_err,
        cv_error_lat_cm=cv_lat,
        cv_error_lon_cm=cv_lon,
        n_ep_used=best_n,
        n_ep_cv=len(candidates),
        n_used=best_n,
        p_used=best_p,
        wk_used=wk,
        ep_nearest=ep_nearest,
        radius_km=current_radius,
    )
