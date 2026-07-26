#!/usr/bin/env python3
"""
Wetter-Service für Ham Radio LoRa Integration - HYBRID VERSION
DWD BrightSky als Primärquelle + OpenMeteo für fehlende Parameter
Intelligente Daten-Fusion für optimale Genauigkeit
"""

import math
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .logging_setup import get_logger

_RAIN_REPORT_MIN_MM = 0.1
_MAX_LORA_MSG_LEN = 149

HTTP_TIMEOUT_S = 10
MAX_RETRIES = 2
RETRY_DELAY_S = 1
WEATHER_CACHE_TTL_S = 300  # SSE-06: 5 min — matches typical weather-update cadence
# Negative cache: error results are cached briefly so an API outage doesn't make
# every queued waiter run its own full ~96 s fetch serially under _cache_lock
# (each parking a thread in the shared default executor).
WEATHER_ERROR_CACHE_TTL_S = 60

_MAGNUS_A = 17.27
_MAGNUS_B = 237.7
_MAGNUS_E0_HPA = 6.112

_PERCENT_PER_OKTA = 12.5
_CALM_WIND_KMH = 1
_ERROR_PREVIEW_LEN = 25

# Data-quality ladder: (threshold %, label). Highest threshold first.
_QUALITY_LADDER = (
    (100, "Exzellent (alle Parameter)"),
    (80, "Sehr gut (fast alle Parameter)"),
    (60, "Gut (wichtigste Parameter)"),
    (40, "Ausreichend (Grundparameter)"),
)

_BERLIN_TZ = ZoneInfo("Europe/Berlin")


def _messzeitpunkt_to_utc(timestamp_str: str) -> datetime:
    """Parse a messzeitpunkt string to an aware UTC datetime.

    Bright Sky returns offset-aware ISO timestamps; Open-Meteo returns naive ISO
    timestamps in the requested "Europe/Berlin" timezone (the API request pins
    "timezone": "Europe/Berlin") — interpreting those as a fixed UTC+1/+2 offset
    is wrong for half the year, so this uses zoneinfo for DST-aware conversion.
    """
    parsed = datetime.fromisoformat(timestamp_str)
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed.replace(tzinfo=_BERLIN_TZ).astimezone(UTC)


def _solar_altitude_deg(lat: float, lon: float, dt_utc: datetime) -> float:
    """Altitude of the sun above the horizon, in degrees, at a location and UTC
    time. NOAA low-precision solar-position algorithm — accurate to a few
    arc-minutes, far beyond what a day/night label needs. A value > 0 means the
    sun is up (daytime). Replaces the old fixed clock window, which mislabelled
    dark winter evenings (e.g. 17:00) as daytime -> "sonnig" at night.
    """
    day_of_year = dt_utc.timetuple().tm_yday
    hour = dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_min = hour * 60 + eqtime + 4 * lon  # lon east-positive
    hour_angle = math.radians(true_solar_min / 4 - 180)
    lat_rad = math.radians(lat)
    cos_zenith = math.sin(lat_rad) * math.sin(decl) + math.cos(lat_rad) * math.cos(decl) * math.cos(
        hour_angle
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))


logger = get_logger(__name__)


class WeatherServiceError(Exception):
    """Custom Exception für Wetter-Service Fehler"""


class WeatherService:
    """
    Hybrid Wetter-Service: DWD primär + OpenMeteo für fehlende Parameter
    Optimale Datenqualität durch intelligente Fusion
    """

    def __init__(
        self,
        lat: float | None = None,
        lon: float | None = None,
        stat_name: str = "",
        max_age_minutes: int = 30,
    ) -> None:
        self.lat = lat
        self.lon = lon
        self.stat_name = stat_name

        # Maximales Alter der Wetterdaten in Minuten
        self.max_age_minutes = max_age_minutes

        # Request timeout und retry config
        self.timeout = HTTP_TIMEOUT_S
        self.max_retries = MAX_RETRIES

        # SSE-06: TTL cache + single-flight guard. get_weather_data() runs via
        # asyncio.to_thread() from multiple concurrent REST requests, so this is a
        # real threading.Lock (not asyncio.Lock) — held across the whole fetch when
        # the cache is stale, so concurrent callers block on the one in-flight
        # fetch instead of each hitting the weather APIs.
        self._cache: dict[str, Any] | None = None
        self._cache_time: float = 0.0
        self._cache_lock = threading.Lock()
        # Monotonic location generation. Bumped by update_location() WITHOUT taking
        # _cache_lock (see that method for why) and compared here so a cache entry
        # — or an in-flight fetch — that belongs to a superseded location is discarded.
        self._cache_generation: int = 0
        self._cached_generation: int = -1

        logger.info(
            "WeatherService initialisiert für %s %s/%s, Hybrid-Modus: DWD + OpenMeteo",
            self.stat_name,
            self.lat,
            self.lon,
        )

    def update_location(self, lat: float, lon: float, stat_name: str | None = None) -> None:
        """Update location from GPS device data"""
        self.lat = lat
        self.lon = lon
        if stat_name:
            self.stat_name = stat_name
        # A stale cache would otherwise keep serving weather for the old location.
        #
        # Deliberately LOCK-FREE: this method is synchronous and is called directly
        # on the asyncio thread (main.py's `_cache_gps` ble_notification subscriber
        # and sse_routes/weather.py's `/api/weather` handler), while
        # get_weather_data() holds `_cache_lock` across the whole blocking
        # `_fetch_weather_data()` — up to ~96 s when the weather APIs are slow, the
        # documented degraded state on a Pi with no internet. Acquiring the lock
        # here froze the entire event loop for that long: SSE heartbeats stopped,
        # UDP ingest stalled, BLE notifications queued. Bumping a counter instead
        # invalidates the cache in O(1) and never blocks; the writer is always the
        # single event-loop thread, the reader is the worker thread.
        self._cache_generation += 1

    def get_weather_data(self, *, bypass_cache: bool = False) -> dict[str, Any]:
        """Cached hybrid weather fetch (SSE-06).

        `bypass_cache=True` is for the mesh `!wx` command (weather_command.py)
        — a ham operator asking for weather over LoRa expects a live reading,
        not a REST-poll-driven cache. REST callers (sse_routes/weather.py's
        `/api/weather` and `/api/weather/preview`) use the default cached path.
        Error responses (no GPS, all APIs down) are cached too, but only for
        WEATHER_ERROR_CACHE_TTL_S (negative caching): waiters queued behind the
        first failing fetch get the cached error back immediately instead of
        each running their own full fetch serially, while a transient failure
        still can't outlive its cause by more than the short TTL.
        """
        if bypass_cache:
            return self._fetch_weather_data()

        with self._cache_lock:
            generation = self._cache_generation
            if self._cache is not None and self._cached_generation == generation:
                ttl = WEATHER_ERROR_CACHE_TTL_S if "error" in self._cache else WEATHER_CACHE_TTL_S
                if (time.monotonic() - self._cache_time) < ttl:
                    return self._cache
            data = self._fetch_weather_data()
            # Only publish the result if the location didn't move while we fetched —
            # otherwise this would re-cache weather for the superseded coordinates.
            if self._cache_generation == generation:
                self._cache = data
                self._cache_time = time.monotonic()
                self._cached_generation = generation
            return data

    def _fetch_weather_data(self) -> dict[str, Any]:  # noqa: PLR0912, PLR0915 - complex handler kept intact
        """
        Hybrid-Methode: DWD primär, OpenMeteo für fehlende Parameter
        """
        if self.lat is None or self.lon is None:
            return {
                "error": "Keine GPS-Position verfügbar (warte auf Gerät)",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        logger.debug("Starte Hybrid-Wetterabfrage...")

        # 1. Versuche DWD BrightSky zu laden
        dwd_data = None
        try:
            logger.debug("📡 Lade DWD BrightSky Daten...")
            dwd_data = self._get_brightsky_weather()

            # Zeitvalidierung für DWD
            age_check = self._validate_data_age(dwd_data)
            if age_check["valid"]:
                if not self._has_valid_core_data(dwd_data):
                    logger.warning("❌ DWD liefert None für Kernparameter → Fallback auf OpenMeteo")
                    dwd_data = None  # DWD verwerfen
                else:
                    logger.debug(
                        "✅ DWD-Daten verfügbar und aktuell (%.1f Min alt)",
                        age_check["age_minutes"],
                    )

            elif not self._has_valid_core_data(dwd_data):
                logger.debug("⚠️  DWD liefert None-Werte → Fallback auf OpenMeteo")
                dwd_data = None
            else:
                logger.debug("⚠️  DWD-Daten zu alt: %s", age_check["reason"])
                dwd_data = None  # Verwerfe alte DWD-Daten

        except Exception as e:
            logger.warning("❌ DWD BrightSky nicht verfügbar: %s", e)
            dwd_data = None

        # 2. Lade OpenMeteo Daten (immer als Backup/Ergänzung)
        openmeteo_data = None
        try:
            logger.debug("📡 Lade OpenMeteo Daten...")
            openmeteo_data = self._get_openmeteo_weather()
            logger.debug("✅ OpenMeteo-Daten verfügbar")
        except Exception as e:
            logger.warning("❌ OpenMeteo nicht verfügbar: %s", e)
            openmeteo_data = None

        # 3. Daten-Fusion: Bestes aus beiden Welten
        if dwd_data is None and openmeteo_data is None:
            # Kompletter Fehler
            return {
                "error": "Alle Wetter-APIs nicht verfügbar",
                "timestamp": datetime.now(UTC).isoformat(),
                "location": f"{self.lat}/{self.lon}",
            }
        if dwd_data is None:
            # Nur OpenMeteo verfügbar (openmeteo_data is not None at this point)
            logger.debug("🔄 Nutze ausschließlich OpenMeteo")
            if openmeteo_data is None:
                raise RuntimeError("openmeteo_data is unexpectedly None")
            openmeteo_data["data_source"] = "OpenMeteo (Fallback)"
            openmeteo_data["timestamp"] = datetime.now(UTC).isoformat()
            return openmeteo_data
        if openmeteo_data is None:
            # Nur DWD verfügbar (dwd_data is not None at this point)
            logger.debug("🔄 Nutze ausschließlich DWD (OpenMeteo nicht verfügbar)")
            if dwd_data is None:
                raise RuntimeError("dwd_data is unexpectedly None")
            dwd_data["data_source"] = "DWD_BrightSky (ohne Ergänzung)"
            dwd_data["timestamp"] = datetime.now(UTC).isoformat()
            return dwd_data
        # Beide verfügbar - FUSION!
        logger.debug("🔄 Führe Daten-Fusion durch: DWD primär + OpenMeteo Ergänzung")
        if dwd_data is None:
            raise RuntimeError("dwd_data is unexpectedly None")
        if openmeteo_data is None:
            raise RuntimeError("openmeteo_data is unexpectedly None")
        fused_data = self._fuse_weather_data(dwd_data, openmeteo_data)
        fused_data["timestamp"] = datetime.now(UTC).isoformat()
        return fused_data

    def _has_valid_core_data(self, weather_data: dict[str, Any]) -> bool:
        """
        Prüfe ob die wichtigsten Wetterdaten verfügbar sind
        Wenn DWD None für Kernparameter liefert → Fallback auf OpenMeteo
        """
        # Definiere kritische Kernparameter
        core_params = [("temperatur_celsius", "Temperatur"), ("luftdruck_hpa", "Luftdruck")]

        invalid_params = []

        for param, param_name in core_params:
            value = weather_data.get(param)
            if value is None:
                invalid_params.append(param_name)
                logger.debug("❌ DWD %s: None", param_name)
            else:
                logger.debug("✅ DWD %s: %s", param_name, value)

        if invalid_params:
            logger.debug(
                "❌ DWD liefert None für kritische Parameter: %s",
                ", ".join(invalid_params),
            )
            return False

        logger.debug("✅ DWD Kernparameter sind gültig")
        return True

    def _fuse_weather_data(
        self,
        dwd_data: dict[str, Any],
        openmeteo_data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Intelligente Daten-Fusion: DWD hat Priorität, OpenMeteo ergänzt fehlende Werte
        """
        logger.debug("🧩 Starte intelligente Daten-Fusion...")

        # Basis: DWD-Daten kopieren
        fused = dwd_data.copy()

        # Liste der kritischen Parameter die ergänzt werden können
        parameters_to_supplement = [
            ("windgeschwindigkeit_kmh", "Wind-Geschwindigkeit"),
            ("windrichtung_grad", "Wind-Richtung"),
            ("windboeen_kmh", "Windböen"),
            ("wolkenbedeckung_prozent", "Wolkenbedeckung"),
            ("sichtweite_meter", "Sichtweite"),
            ("niederschlag_mm", "Niederschlag"),
            ("luftfeuchtigkeit_prozent", "Luftfeuchtigkeit"),
        ]

        supplemented_params = []
        kept_dwd_params = []

        for param, param_name in parameters_to_supplement:
            dwd_value = dwd_data.get(param)
            openmeteo_value = openmeteo_data.get(param)

            if dwd_value is None and openmeteo_value is not None:
                # DWD hat keinen Wert, OpenMeteo ergänzt
                fused[param] = openmeteo_value
                supplemented_params.append(param_name)
                logger.debug("  ➕ %s: %s (von OpenMeteo ergänzt)", param_name, openmeteo_value)
            elif dwd_value is not None:
                # DWD-Wert behalten
                kept_dwd_params.append(f"{param_name}: {dwd_value}")
                logger.debug("  ✅ %s: %s (DWD behalten)", param_name, dwd_value)
            else:
                # Beide None
                logger.debug("  ⚠️  %s: Nicht verfügbar", param_name)

        # Datenquellen-Info zusammenstellen
        if supplemented_params:
            source_info = f"DWD_BrightSky + OpenMeteo ({', '.join(supplemented_params)})"
            logger.debug(
                "✅ Fusion abgeschlossen: %d Parameter von OpenMeteo ergänzt",
                len(supplemented_params),
            )
        else:
            source_info = "DWD_BrightSky (vollständig)"
            logger.debug("✅ Fusion abgeschlossen: DWD-Daten waren vollständig")

        fused["data_source"] = source_info
        fused["supplemented_parameters"] = supplemented_params

        # Qualitätsbewertung
        fused["data_quality"] = self._assess_data_quality(fused)

        return fused

    def _assess_data_quality(self, weather_data: dict[str, Any]) -> str:
        """
        Bewerte die Qualität der fusionierten Daten
        """
        # Kritische Parameter prüfen
        critical_params = [
            "temperatur_celsius",
            "luftfeuchtigkeit_prozent",
            "luftdruck_hpa",
            "windgeschwindigkeit_kmh",
            "wolkenbedeckung_prozent",
        ]

        available_critical = sum(
            1 for param in critical_params if weather_data.get(param) is not None
        )
        total_critical = len(critical_params)

        quality_score = (available_critical / total_critical) * 100

        for threshold, label in _QUALITY_LADDER:
            if quality_score >= threshold:
                return label
        return "Unvollständig (kritische Parameter fehlen)"

    def _validate_data_age(self, weather_data: dict[str, Any]) -> dict[str, Any]:
        """Validierung des Datenalters"""
        messzeitpunkt_str = weather_data.get("messzeitpunkt")

        if not messzeitpunkt_str or messzeitpunkt_str == "unbekannt":
            return {
                "valid": False,
                "age_minutes": float("inf"),
                "reason": "Kein Messzeitpunkt verfügbar",
            }

        try:
            measurement_time = _messzeitpunkt_to_utc(messzeitpunkt_str)
            now = datetime.now(UTC)
            age_delta = now - measurement_time
            age_minutes = age_delta.total_seconds() / 60

            if age_minutes < 0:
                return {
                    "valid": False,
                    "age_minutes": abs(age_minutes),
                    "reason": f"Daten sind {abs(age_minutes):.1f} Min in der Zukunft (Forecast)",
                }

            is_valid = age_minutes <= self.max_age_minutes
            return {
                "valid": is_valid,
                "age_minutes": age_minutes,
                "reason": (
                    f"Daten sind {age_minutes:.1f} Min alt"
                    + ("" if is_valid else f" (> {self.max_age_minutes} Min)")
                ),
            }

        except (ValueError, TypeError) as e:
            return {
                "valid": False,
                "age_minutes": float("inf"),
                "reason": f"Ungültiger Messzeitpunkt: {e}",
            }

    def _calculate_humidity_from_dewpoint(
        self,
        temperature_c: float,
        dewpoint_c: float,
    ) -> int | None:
        """Berechne relative Luftfeuchtigkeit aus Temperatur und Taupunkt"""
        try:
            a, b = _MAGNUS_A, _MAGNUS_B
            alpha_temp = (a * temperature_c) / (b + temperature_c)
            es_temp = _MAGNUS_E0_HPA * math.exp(alpha_temp)
            alpha_dew = (a * dewpoint_c) / (b + dewpoint_c)
            es_dew = _MAGNUS_E0_HPA * math.exp(alpha_dew)
            relative_humidity = (es_dew / es_temp) * 100
            return round(max(0, min(100, relative_humidity)))
        except (ValueError, ZeroDivisionError, OverflowError):
            return None

    def _get_brightsky_weather(self) -> dict[str, Any]:
        """DWD Bright Sky API"""
        urls_to_try: list[dict[str, Any]] = [
            {
                "url": "https://api.brightsky.dev/current_weather",
                "params": {"lat": self.lat, "lon": self.lon},
                "name": "current_weather",
            },
            {
                "url": "https://api.brightsky.dev/weather",
                "params": {
                    "lat": self.lat,
                    "lon": self.lon,
                    "date": datetime.now().astimezone().strftime("%Y-%m-%d"),
                    "last": 24,
                },
                "name": "weather_recent",
            },
        ]

        for endpoint in urls_to_try:
            try:
                response = self._make_request(str(endpoint["url"]), dict(endpoint["params"]))
                data = response.json()

                if not isinstance(data, dict) or "weather" not in data:
                    continue

                weather_records = data["weather"]

                current: dict[str, Any] | None
                if isinstance(weather_records, dict):
                    current = weather_records
                elif isinstance(weather_records, list) and len(weather_records) > 0:
                    current = self._find_most_recent_record(weather_records)
                else:
                    continue

                if not current:
                    continue

                result = self._process_brightsky_record(current, data)
                if result:
                    return result

            except Exception as e:
                logger.debug("BrightSky %s fehlgeschlagen: %s", endpoint["name"], e)
                continue

        raise WeatherServiceError("Alle BrightSky Endpunkte fehlgeschlagen")

    def _find_most_recent_record(
        self, weather_records: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Finde den aktuellsten Datensatz"""
        if not weather_records:
            return None

        try:

            def parse_timestamp(record: dict[str, Any]) -> datetime:
                ts_str = record.get("timestamp", "")
                if ts_str:
                    return datetime.fromisoformat(ts_str)
                return datetime.min.replace(tzinfo=UTC)

            sorted_records = sorted(weather_records, key=parse_timestamp, reverse=True)
            now = datetime.now(UTC)

            for record in sorted_records:
                try:
                    record_time = parse_timestamp(record)
                    if record_time <= now:
                        return record
                except Exception as e:
                    logger.debug("Skipping unparsable weather record: %s", e)
                    continue

            return sorted_records[0] if sorted_records else None
        except Exception:
            return weather_records[0] if weather_records else None

    def _process_brightsky_record(
        self, current: dict[str, Any], full_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Verarbeite einen BrightSky Wetter-Record"""
        # Station Info
        station_name = "unbekannt"
        if "sources" in full_data and full_data["sources"] and len(full_data["sources"]) > 0:
            first_source = full_data["sources"][0]
            if isinstance(first_source, dict):
                station_name = first_source.get("station_name", "unbekannt")

        # Basis-Daten extrahieren
        temperature = self._safe_float(current.get("temperature"))
        dewpoint = self._safe_float(current.get("dew_point"))
        relative_humidity_raw = current.get("relative_humidity")

        # Luftfeuchtigkeit
        luftfeuchtigkeit = None
        if relative_humidity_raw is not None:
            luftfeuchtigkeit = self._safe_int(relative_humidity_raw)
        elif temperature is not None and dewpoint is not None:
            luftfeuchtigkeit = self._calculate_humidity_from_dewpoint(temperature, dewpoint)

        return {
            "temperatur_celsius": temperature,
            "luftfeuchtigkeit_prozent": luftfeuchtigkeit,
            "luftdruck_hpa": self._safe_float(current.get("pressure_msl")),
            "windgeschwindigkeit_kmh": self._safe_float(current.get("wind_speed")),
            "windrichtung_grad": self._safe_int(current.get("wind_direction")),
            "wolkenbedeckung_prozent": self._safe_int(current.get("cloud_cover")),
            "sichtweite_meter": self._safe_int(current.get("visibility")),
            "niederschlag_mm": self._safe_float(current.get("precipitation")),
            "bedingung": current.get("condition", "unbekannt"),
            "dwd_station": station_name,
            "messzeitpunkt": current.get("timestamp", "unbekannt"),
            "taupunkt_celsius": dewpoint,
        }

    def _get_openmeteo_weather(self) -> dict[str, Any]:
        """Open-Meteo API"""
        url = "https://api.open-meteo.com/v1/forecast"
        params: dict[str, Any] = {
            "latitude": self.lat,
            "longitude": self.lon,
            "current": (
                "temperature_2m,relative_humidity_2m,pressure_msl,"
                "cloud_cover,wind_speed_10m,wind_direction_10m,"
                "wind_gusts_10m,visibility,precipitation"
            ),
            "timezone": "Europe/Berlin",
        }

        response = self._make_request(url, params)
        data = response.json()

        logger.debug("openmeteo response: %s", data)

        if "current" not in data:
            raise WeatherServiceError("Keine aktuellen Open-Meteo-Daten verfügbar")

        current = data["current"]

        return {
            "temperatur_celsius": self._safe_float(current.get("temperature_2m")),
            "luftfeuchtigkeit_prozent": self._safe_int(current.get("relative_humidity_2m")),
            "luftdruck_hpa": self._safe_float(current.get("pressure_msl")),
            "windgeschwindigkeit_kmh": self._safe_float(current.get("wind_speed_10m")),
            "windrichtung_grad": self._safe_int(current.get("wind_direction_10m")),
            "windboeen_kmh": self._safe_float(current.get("wind_gusts_10m")),
            "wolkenbedeckung_prozent": self._safe_int(current.get("cloud_cover")),
            "sichtweite_meter": self._safe_int(current.get("visibility")),
            "niederschlag_mm": self._safe_float(current.get("precipitation")),
            "bedingung": "N/A",
            "dwd_station": "Open-Meteo Modell",
            "messzeitpunkt": current.get("time", "unbekannt"),
        }

    def _make_request(self, url: str, params: dict[str, Any]) -> httpx.Response:
        """Robuste HTTP-Request mit Retry-Logic"""
        for attempt in range(self.max_retries + 1):
            try:
                response = httpx.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                    headers={"User-Agent": "HamRadio-WeatherService/1.0"},
                )
                response.raise_for_status()
            except httpx.TimeoutException as e:
                if attempt == self.max_retries:
                    raise WeatherServiceError("Request Timeout") from e
                time.sleep(RETRY_DELAY_S)
            except httpx.HTTPError as e:
                if attempt == self.max_retries:
                    raise WeatherServiceError(f"HTTP-Fehler: {e}") from e
                time.sleep(RETRY_DELAY_S)
            else:
                return response
        raise WeatherServiceError("Request failed after retries")

    def _safe_float(self, value: Any) -> float | None:
        """Sichere Float-Konvertierung"""
        try:
            return float(value) if value is not None else None
        except (ValueError, TypeError):
            return None

    def _safe_int(self, value: Any) -> int | None:
        """Sichere Int-Konvertierung"""
        try:
            return int(float(value)) if value is not None else None
        except (ValueError, TypeError):
            return None

    def _is_daytime(self, timestamp_str: str | None) -> bool:
        """True if the sun is above the horizon at the station location and the
        reference time — real sunrise/sunset, not a fixed clock window (which
        labelled dark winter evenings as day -> "sonnig" at night). Reference
        time is the measurement timestamp when parseable, else now; without a
        location we cannot compute it and assume day.
        """
        if self.lat is None or self.lon is None:
            return True
        ref: datetime | None = None
        if timestamp_str and timestamp_str != "unbekannt":
            try:
                ref = _messzeitpunkt_to_utc(timestamp_str)
            except (ValueError, TypeError):
                ref = None
        if ref is None:
            ref = datetime.now(UTC)
        return _solar_altitude_deg(self.lat, self.lon, ref) > 0.0

    def _calculate_cloud_coverage_description(
        self,
        cloud_percent: float | None,
        timestamp_str: str | None = None,
    ) -> str:
        """Berechne Wolkenbedeckung in Achteln (/8) und Beschreibung"""
        if cloud_percent is None:
            return "unbekannt"

        eighths = round(cloud_percent / _PERCENT_PER_OKTA)
        eighths = max(0, min(8, eighths))
        is_day = self._is_daytime(timestamp_str)

        if eighths == 0:
            return "sonnig" if is_day else "klar"
        if eighths <= 1:
            return f"{eighths}/8 (heiter)" if is_day else f"{eighths}/8 (überwiegend klar)"
        if eighths <= 3:  # noqa: PLR2004 - okta ladder
            return f"{eighths}/8 (aufgelockert bewölkt)"
        if eighths <= 6:  # noqa: PLR2004 - okta ladder
            return f"{eighths}/8 (teilweise bewölkt)"
        return "bewölkt"

    def format_for_lora(self, weather_data: dict[str, Any], prefix_text: str = "") -> str:
        """Ham Radio optimiertes LoRa-Format"""
        if "error" in weather_data:
            return f"WX ERR: {weather_data['error'][:_ERROR_PREVIEW_LEN]}"

        temp = weather_data.get("temperatur_celsius", 0)
        humid = weather_data.get("luftfeuchtigkeit_prozent", 0)
        press = weather_data.get("luftdruck_hpa", 0)

        if temp is None:
            temp = 0.0
            logger.debug("⚠️  Temperatur None → 0.0")
        if humid is None:
            humid = 0
            logger.debug("⚠️  Luftfeuchtigkeit None → 0")
        if press is None:
            press = 0.0
            logger.debug("⚠️  Luftdruck None → 0.0")

        # Wind
        wind_speed = weather_data.get("windgeschwindigkeit_kmh", 0) or 0
        wind_dir = weather_data.get("windrichtung_grad")

        if wind_speed >= _CALM_WIND_KMH:
            wind_compass = self._wind_direction_to_compass(wind_dir)
            if wind_compass:
                wind_info = f"Wind {wind_speed:.1f}km/h {wind_compass}"
            else:
                wind_info = f"Wind {wind_speed:.1f}km/h"
        else:
            wind_info = "windstill"

        # Wolkenbedeckung
        clouds_percent = weather_data.get("wolkenbedeckung_prozent")
        cloud_desc = self._calculate_cloud_coverage_description(
            clouds_percent,
            weather_data.get("messzeitpunkt"),
        )

        # Niederschlag ist optional
        rain_mm = weather_data.get("niederschlag_mm", 0) or 0
        rain_info = f", {rain_mm:.1f}mm rain" if rain_mm > _RAIN_REPORT_MIN_MM else ""

        # Personal text prefix
        prefix = f"{prefix_text} " if prefix_text else ""

        lora_msg = (
            f"{prefix}🌤️ WX {self.stat_name}: {temp:.1f}C {humid}% rF,"
            f" {press:.1f}hPa, {wind_info}, {cloud_desc}{rain_info}"
        )

        if len(lora_msg) > _MAX_LORA_MSG_LEN:
            lora_msg = (
                f"{prefix}WX {self.stat_name}: {temp:.1f}C {humid}%rF"
                f" {press:.1f}hPa {wind_info} {cloud_desc}{rain_info}"
            )

        return lora_msg

    def _wind_direction_to_compass(self, degrees: int | None) -> str:
        """
        Konvertiere Windrichtung von Grad zu Himmelsrichtung
        232° → SW
        """
        if degrees is None:
            return ""

        # Normalisiere auf 0-359°
        degrees = degrees % 360

        # 16 Himmelsrichtungen für präzise Angabe
        directions = [
            "N",
            "NNE",
            "NE",
            "ENE",
            "E",
            "ESE",
            "SE",
            "SSE",
            "S",
            "SSW",
            "SW",
            "WSW",
            "W",
            "WNW",
            "NW",
            "NNW",
        ]

        # Jede Richtung umfasst 22.5° (360° / 16)
        # +11.25° für Rundung zur nächsten Richtung
        index = round((degrees + 11.25) / 22.5) % 16

        return directions[index]

    def get_verbose_report(self, weather_data: dict[str, Any]) -> str:
        """Ausführlicher Wetterbericht mit Fusion-Details"""
        if "error" in weather_data:
            return f"❌ FEHLER: {weather_data['error']}"

        # Basis-Info
        temp = weather_data.get("temperatur_celsius", "N/A")
        humid = weather_data.get("luftfeuchtigkeit_prozent", "N/A")
        press = weather_data.get("luftdruck_hpa", "N/A")

        # Wind-Info
        wind_speed = weather_data.get("windgeschwindigkeit_kmh")
        wind_dir = weather_data.get("windrichtung_grad")
        if wind_speed is not None and wind_dir is not None:
            wind_text = f"{wind_speed:.1f} km/h aus {wind_dir}°"
        elif wind_speed is not None:
            wind_text = f"{wind_speed:.1f} km/h"
        else:
            wind_text = "N/A"

        # Wolken-Info
        clouds_percent = weather_data.get("wolkenbedeckung_prozent")
        cloud_desc = self._calculate_cloud_coverage_description(
            clouds_percent,
            weather_data.get("messzeitpunkt"),
        )
        cloud_text = f"{clouds_percent}% ({cloud_desc})" if clouds_percent is not None else "N/A"

        # Fusion-Info
        fusion_info = ""
        if weather_data.get("supplemented_parameters"):
            supplemented = ", ".join(weather_data["supplemented_parameters"])
            fusion_info = f"🔗 Fusion:         {supplemented} von OpenMeteo ergänzt\n"

        quality_info = ""
        if "data_quality" in weather_data:
            quality_info = f"⭐ Qualität:       {weather_data['data_quality']}\n"

        # Zusätzliche Infos
        extra_info = ""
        if (
            weather_data.get("data_source", "").startswith("DWD")
            and "taupunkt_celsius" in weather_data
        ):
            extra_info = f"🌡️ Taupunkt:       {weather_data.get('taupunkt_celsius', 'N/A')}°C\n"

        # Niederschlag
        rain_mm = weather_data.get("niederschlag_mm", 0) or 0
        rain_info = f"🌧️  Niederschlag:   {rain_mm:.1f} mm\n" if rain_mm > 0 else ""

        report = f"""
🌤️  {self.stat_name} {self.lat}/{self.lon} - {weather_data.get("timestamp", "N/A")[:19]}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🌡️  Temperatur:     {temp}°C
💧  Luftfeuchtigkeit: {humid}%
{extra_info}🔽  Luftdruck:      {press} hPa
💨  Wind:           {wind_text}
☁️  Wolkenbedeckung: {cloud_text}
👁️  Sichtweite:     {weather_data.get("sichtweite_meter", "N/A")} m
{rain_info}🏢  Station:        {weather_data.get("dwd_station", "N/A")}
📡  Quelle:         {weather_data.get("data_source", "N/A")}
{fusion_info}{quality_info}⏰  Messzeitpunkt:  {weather_data.get("messzeitpunkt", "N/A")[:19]}

📻 LoRa Format: {self.format_for_lora(weather_data)}
        """
        return report.strip()


def main() -> None:
    """Produktions-Version"""
    # Freising
    stat_name = "Freising"
    lat = 48.4031
    lon = 11.7497

    # Alternative Station: Leonding, OÖ — 48.279331 N, 14.248746 E

    print("🚀 Ham Radio Wetter-Service - HYBRID VERSION")
    print("🔗 DWD BrightSky primär + OpenMeteo Ergänzung")
    print(f"📍 Standort: {lat}/{lon}")
    print("-" * 70)

    weather_service = WeatherService(lat, lon, stat_name, max_age_minutes=30)

    try:
        weather_data = weather_service.get_weather_data()
        print(weather_service.get_verbose_report(weather_data))

        if "error" not in weather_data:
            lora_packet = weather_service.format_for_lora(weather_data)
            print("\n📦 LoRa Ham Radio Nachricht:")
            print(f"   {lora_packet}")
            print(f"📏 Länge: {len(lora_packet)} Zeichen")

    except KeyboardInterrupt:
        print("\n🛑 Test durch Benutzer abgebrochen")
    except Exception:
        logger.exception("Unerwarteter Fehler")
        sys.exit(1)


if __name__ == "__main__":
    from .logging_setup import setup_logging

    setup_logging()
    main()
