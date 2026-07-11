# ruff: noqa: SLF001
# This suite intentionally exercises WeatherService's private pure-logic helpers
# (_fuse_weather_data, _validate_data_age, _calculate_humidity_from_dewpoint,
# _calculate_cloud_coverage_description, _wind_direction_to_compass) directly —
# they have no public wrapper and testing them is this file's entire purpose.
# A single file-level suppression here stands in for the SLF001 exemption that
# pyproject.toml already grants to **/tests.py and **/test_*.py; this filename
# just doesn't match either glob.
"""Built-in test suite for the pure, network-free logic in meteo.py.

Covers weather-formatting computations that never touch the network:
data-source fusion precedence, data-age validation, dewpoint→humidity (Magnus
formula), cloud-cover percent→okta conversion, 16-point compass bucketing, and
the LoRa message formatter (including its length-based fallback format).

Only tz handling (_messzeitpunkt_to_utc / _is_daytime) and the negative-cache
behaviour of get_weather_data() are covered elsewhere, in
commands/tests.py::test_meteo_timezone_validators/test_meteo_negative_cache.
"""

import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from .meteo import _MAX_LORA_MSG_LEN, WeatherService

results: list[tuple[str, str]] = []


def _check(label: str, actual: object, expected: object) -> None:
    ok = actual == expected
    status = "✅ PASS" if ok else "❌ FAIL"
    results.append((status, label))
    print(f"{status} | {label}")
    if not ok:
        print(f"     actual={actual!r} expected={expected!r}")


def _test_fuse_weather_data() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="FuseTest")

    dwd_data: dict[str, Any] = {
        "temperatur_celsius": 10.0,
        "luftdruck_hpa": 1000.0,
        "windgeschwindigkeit_kmh": None,
        "windrichtung_grad": 180,
        "windboeen_kmh": None,
        "wolkenbedeckung_prozent": 50,
        "sichtweite_meter": None,
        "niederschlag_mm": 0.0,
        "luftfeuchtigkeit_prozent": 60,
    }
    openmeteo_data: dict[str, Any] = {
        "temperatur_celsius": 99.0,  # not in the supplement list -> must be ignored
        "windgeschwindigkeit_kmh": 20.0,  # DWD None -> supplemented
        "windrichtung_grad": 270,  # DWD has a value -> DWD wins
        "windboeen_kmh": 5.0,  # DWD None -> supplemented
        "wolkenbedeckung_prozent": 80,  # DWD has a value -> DWD wins
        "sichtweite_meter": 1000,  # DWD None -> supplemented
        "niederschlag_mm": None,  # DWD has a value (0.0, not None) -> DWD wins
        "luftfeuchtigkeit_prozent": None,  # DWD has a value -> DWD wins
    }

    fused = ws._fuse_weather_data(dwd_data, openmeteo_data)

    _check(
        "fuse: field outside supplement list keeps DWD value even if OpenMeteo differs",
        fused["temperatur_celsius"],
        10.0,
    )
    _check(
        "fuse: DWD None + OpenMeteo value -> supplemented",
        fused["windgeschwindigkeit_kmh"],
        20.0,
    )
    _check(
        "fuse: DWD value + OpenMeteo value -> DWD kept (wind direction)",
        fused["windrichtung_grad"],
        180,
    )
    _check(
        "fuse: DWD None + OpenMeteo value -> supplemented (gusts)",
        fused["windboeen_kmh"],
        5.0,
    )
    _check(
        "fuse: DWD value + OpenMeteo value -> DWD kept (clouds)",
        fused["wolkenbedeckung_prozent"],
        50,
    )
    _check(
        "fuse: DWD None + OpenMeteo value -> supplemented (visibility)",
        fused["sichtweite_meter"],
        1000,
    )
    _check(
        "fuse: DWD 0.0 (not None) + OpenMeteo None -> DWD's 0.0 kept, not overwritten",
        fused["niederschlag_mm"],
        0.0,
    )
    _check(
        "fuse: DWD value + OpenMeteo None -> DWD kept (humidity)",
        fused["luftfeuchtigkeit_prozent"],
        60,
    )
    _check(
        "fuse: supplemented_parameters lists exactly the OpenMeteo-filled fields, in order",
        fused["supplemented_parameters"],
        ["Wind-Geschwindigkeit", "Windböen", "Sichtweite"],
    )
    _check(
        "fuse: data_source names the supplemented fields",
        fused["data_source"],
        "DWD_BrightSky + OpenMeteo (Wind-Geschwindigkeit, Windböen, Sichtweite)",
    )
    _check(
        "fuse: all 5 quality-critical params present -> top quality label",
        fused["data_quality"],
        "Exzellent (alle Parameter)",
    )

    # Fully-DWD-complete case: nothing to supplement.
    complete_dwd: dict[str, Any] = {
        "windgeschwindigkeit_kmh": 5.0,
        "windrichtung_grad": 90,
        "windboeen_kmh": 8.0,
        "wolkenbedeckung_prozent": 20,
        "sichtweite_meter": 20000,
        "niederschlag_mm": 0.0,
        "luftfeuchtigkeit_prozent": 70,
    }
    fused_complete = ws._fuse_weather_data(complete_dwd, openmeteo_data)
    _check(
        "fuse: DWD fully populated -> nothing supplemented",
        fused_complete["supplemented_parameters"],
        [],
    )
    _check(
        "fuse: DWD fully populated -> data_source says complete",
        fused_complete["data_source"],
        "DWD_BrightSky (vollständig)",
    )


def _test_validate_data_age() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="AgeTest", max_age_minutes=30)
    now = datetime.now(UTC)

    fresh = (now - timedelta(minutes=5)).isoformat()
    fresh_result = ws._validate_data_age({"messzeitpunkt": fresh})
    _check("validate_data_age: 5 min old (< 30 min max) is valid", fresh_result["valid"], True)

    stale = (now - timedelta(minutes=60)).isoformat()
    stale_result = ws._validate_data_age({"messzeitpunkt": stale})
    _check("validate_data_age: 60 min old (> 30 min max) is invalid", stale_result["valid"], False)
    _check(
        "validate_data_age: stale reason mentions the max-age threshold",
        "> 30 Min" in stale_result["reason"],
        True,
    )

    future = (now + timedelta(minutes=10)).isoformat()
    future_result = ws._validate_data_age({"messzeitpunkt": future})
    _check("validate_data_age: 10 min in the future is invalid", future_result["valid"], False)
    _check(
        "validate_data_age: future reason names it a forecast",
        "Forecast" in future_result["reason"],
        True,
    )

    missing_result = ws._validate_data_age({})
    _check("validate_data_age: missing messzeitpunkt is invalid", missing_result["valid"], False)
    _check(
        "validate_data_age: missing messzeitpunkt age is +inf",
        missing_result["age_minutes"],
        float("inf"),
    )

    sentinel_result = ws._validate_data_age({"messzeitpunkt": "unbekannt"})
    _check(
        "validate_data_age: 'unbekannt' sentinel is invalid like missing",
        sentinel_result["valid"],
        False,
    )


def _test_calculate_humidity_from_dewpoint() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="HumidityTest")

    # Magnus formula, temp=20.0C dewpoint=10.0C -> raw RH ~= 52.5658...%, rounds to 53.
    rh = ws._calculate_humidity_from_dewpoint(20.0, 10.0)
    _check("humidity_from_dewpoint: 20.0C/10.0C dewpoint rounds to 53%", rh, 53)

    # Saturated: dewpoint == temperature -> exactly 100%.
    rh_saturated = ws._calculate_humidity_from_dewpoint(15.0, 15.0)
    _check("humidity_from_dewpoint: dewpoint == temperature -> 100%", rh_saturated, 100)

    # Dewpoint above temperature is unphysical but the formula still clamps to <= 100.
    rh_clamped = ws._calculate_humidity_from_dewpoint(5.0, 20.0)
    _check("humidity_from_dewpoint: dewpoint > temperature is clamped to 100%", rh_clamped, 100)


def _test_okta_conversion() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="OktaTest")
    day_ts = "2026-07-15T12:00:00+00:00"
    night_ts = "2026-07-15T00:00:00+00:00"

    _check(
        "okta: None cloud cover -> unbekannt",
        ws._calculate_cloud_coverage_description(None, day_ts),
        "unbekannt",
    )
    _check(
        "okta: 0% at day -> sonnig (0/8, banker's rounding of 0.5 -> 0)",
        ws._calculate_cloud_coverage_description(0, day_ts),
        "sonnig",
    )
    _check(
        "okta: 0% at night -> klar",
        ws._calculate_cloud_coverage_description(0, night_ts),
        "klar",
    )
    _check(
        "okta: 6.25% (round(0.5)==0 banker's rounding) still rounds to 0/8 -> sonnig",
        ws._calculate_cloud_coverage_description(6.25, day_ts),
        "sonnig",
    )
    _check(
        "okta: 12.5% -> 1/8 boundary, day phrasing",
        ws._calculate_cloud_coverage_description(12.5, day_ts),
        "1/8 (heiter)",
    )
    _check(
        "okta: 12.5% -> 1/8 boundary, night phrasing",
        ws._calculate_cloud_coverage_description(12.5, night_ts),
        "1/8 (überwiegend klar)",
    )
    _check(
        "okta: 37.5% -> 3/8 (aufgelockert bewölkt)",
        ws._calculate_cloud_coverage_description(37.5, day_ts),
        "3/8 (aufgelockert bewölkt)",
    )
    _check(
        "okta: 75% -> 6/8 (teilweise bewölkt), upper boundary of that bucket",
        ws._calculate_cloud_coverage_description(75, day_ts),
        "6/8 (teilweise bewölkt)",
    )
    _check(
        "okta: 87.5% -> 7/8 rounds past the 'teilweise bewölkt' bucket -> bewölkt",
        ws._calculate_cloud_coverage_description(87.5, day_ts),
        "bewölkt",
    )
    _check(
        "okta: 100% -> 8/8 -> bewölkt",
        ws._calculate_cloud_coverage_description(100, day_ts),
        "bewölkt",
    )


def _test_compass_conversion() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="CompassTest")

    _check("compass: None -> empty string", ws._wind_direction_to_compass(None), "")
    _check("compass: 0deg -> N", ws._wind_direction_to_compass(0), "N")
    _check("compass: 11deg -> NNE", ws._wind_direction_to_compass(11), "NNE")
    _check(
        "compass: 22deg -> NNE (still within the NNE bucket)",
        ws._wind_direction_to_compass(22),
        "NNE",
    )
    _check(
        "compass: 23deg -> NE (first degree of the NE bucket)",
        ws._wind_direction_to_compass(23),
        "NE",
    )
    _check("compass: 45deg -> NE", ws._wind_direction_to_compass(45), "NE")
    _check("compass: 203deg -> SW", ws._wind_direction_to_compass(203), "SW")
    _check(
        "compass: 225deg -> SW (last degree of the SW bucket)",
        ws._wind_direction_to_compass(225),
        "SW",
    )
    _check(
        "compass: 226deg -> WSW (first degree of the WSW bucket)",
        ws._wind_direction_to_compass(226),
        "WSW",
    )
    _check("compass: 337deg -> NNW", ws._wind_direction_to_compass(337), "NNW")
    _check(
        "compass: 338deg -> N (wraps into the N bucket early)",
        ws._wind_direction_to_compass(338),
        "N",
    )
    _check("compass: 359deg -> N", ws._wind_direction_to_compass(359), "N")
    _check("compass: 360deg normalizes to 0 -> N", ws._wind_direction_to_compass(360), "N")
    _check("compass: 370deg normalizes to 10 -> NNE", ws._wind_direction_to_compass(370), "NNE")
    _check("compass: -10deg normalizes to 350 -> N", ws._wind_direction_to_compass(-10), "N")


def _test_format_for_lora() -> None:
    ws = WeatherService(lat=48.15, lon=11.58, stat_name="TestStation")

    # Normal case, mirrors the canned payload used by commands/tests.py's !WX test.
    canned: dict[str, Any] = {
        "temperatur_celsius": 21.5,
        "luftfeuchtigkeit_prozent": 55,
        "luftdruck_hpa": 1013.2,
        "windgeschwindigkeit_kmh": 0,  # below the calm threshold -> "windstill"
        "timestamp": "test",
    }
    normal = ws.format_for_lora(canned)
    _check(
        "format_for_lora: normal case matches production formatting exactly",
        normal,
        "🌤️ WX TestStation: 21.5C 55% rF, 1013.2hPa, windstill, unbekannt",
    )

    # Error passthrough, truncated to _ERROR_PREVIEW_LEN (25) chars.
    err_msg = "Alle Wetter-APIs nicht verfügbar wegen Netzwerkfehler und Timeout etc etc"
    err_out = ws.format_for_lora({"error": err_msg})
    _check(
        "format_for_lora: error case previews the error message to 25 chars",
        err_out,
        "WX ERR: Alle Wetter-APIs nicht ve",
    )

    # Rich payload whose emoji-format rendering exceeds _MAX_LORA_MSG_LEN chars,
    # forcing the shorter, emoji-free fallback format. Chosen so the *fallback* itself
    # still fits within the cap.
    rich_ws = WeatherService(
        lat=48.15, lon=11.58, stat_name="LONGSTATION123456", max_age_minutes=30
    )
    rich_data: dict[str, Any] = {
        "temperatur_celsius": -12.34,
        "luftfeuchtigkeit_prozent": 87,
        "luftdruck_hpa": 987.65,
        "windgeschwindigkeit_kmh": 45.6,
        "windrichtung_grad": 225,
        "wolkenbedeckung_prozent": 90,
        "niederschlag_mm": 12.3,
        "messzeitpunkt": "2026-07-15T12:00:00+00:00",
    }
    prefix_text = "Hallo Freunde vom Ham Radio Club heute" + "X" * 23
    truncated = rich_ws.format_for_lora(rich_data, prefix_text=prefix_text)

    _check(
        "format_for_lora: rich payload triggers the >149-char fallback format",
        truncated,
        "Hallo Freunde vom Ham Radio Club heuteXXXXXXXXXXXXXXXXXXXXXXX "
        "WX LONGSTATION123456: -12.3C 87%rF 987.6hPa Wind 45.6km/h SW bewölkt, 12.3mm rain",
    )
    _check(
        "format_for_lora: fallback format has no emoji (production checks len(), not bytes)",
        "\U0001f324" in truncated,
        False,
    )
    _check(
        "format_for_lora: fallback format fits within the _MAX_LORA_MSG_LEN char cap",
        len(truncated) <= _MAX_LORA_MSG_LEN,
        True,
    )
    _check(
        "format_for_lora: fallback is still well-formed (station, temp, humidity, "
        "pressure, wind, clouds, rain all present)",
        all(
            token in truncated
            for token in (
                "LONGSTATION123456",
                "-12.3C",
                "87%rF",
                "987.6hPa",
                "Wind 45.6km/h SW",
                "bewölkt",
                "12.3mm rain",
            )
        ),
        True,
    )


def run_meteo_tests() -> bool:
    """Run all pure-logic meteo tests. Returns True iff every case passed."""
    results.clear()  # allow repeated invocations within one process (e.g. re-runs)

    print("Testing meteo.py pure logic:")
    print("=" * 50)

    _test_fuse_weather_data()
    _test_validate_data_age()
    _test_calculate_humidity_from_dewpoint()
    _test_okta_conversion()
    _test_compass_conversion()
    _test_format_for_lora()

    passed = sum(1 for status, _ in results if status.startswith("✅"))
    total = len(results)
    all_ok = passed == total

    print("=" * 50)
    print(f"Test Summary: {passed}/{total} tests passed")
    print(f"meteo: {'PASS' if all_ok else 'FAIL'}")

    return all_ok


if __name__ == "__main__":
    sys.exit(0 if run_meteo_tests() else 1)
