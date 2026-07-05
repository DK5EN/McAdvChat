"""Built-in default classifier rules.

``seed_defaults`` upserts the DEFAULT_RULES.  Matching is by ``name``:
rows that don't exist yet are inserted as builtin; rows whose stored
``pattern``/``category``/``priority``/``extra_tags``/``scope`` have
drifted from the defaults are updated in place.  User rules (builtin=0)
are never touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import StorageProtocol

# Each entry mirrors the kwargs accepted by Storage.insert_classifier_rule.
# All builtins are enabled=True, builtin=True.
DEFAULT_RULES: list[dict[str, Any]] = [
    # ── Bot commands (must match before greeting/directed) ──────────────
    {
        "priority": 5,
        "name": "Bot command dash",
        "scope": "msg",
        "pattern": r"^\s*(—|–|--)[a-zA-Z][a-zA-Z0-9_-]{0,30}(\s.*)?$",
        "category": "bot_command",
        "extra_tags": ["bot"],
    },
    {
        "priority": 6,
        "name": "Bot command help response",
        "scope": "msg",
        "pattern": r"(?i)^(befehle|commands)\s*:\s*--[a-z]",
        "category": "bot_command",
        "extra_tags": ["bot"],
    },
    {
        "priority": 7,
        "name": "Bot error response",
        "scope": "msg",
        "pattern": r"(?i)^unbekannter befehl:",
        "category": "bot_command",
        "extra_tags": ["bot"],
    },
    {
        "priority": 8,
        "name": "Ping command",
        "scope": "msg",
        "pattern": r"(?i)^\s*ping\s*$",
        "category": "bot_command",
        "extra_tags": ["bot"],
    },
    # ── Timestamp beacon ────────────────────────────────────────────────
    {
        "priority": 10,
        "name": "CET timestamp",
        "scope": "msg",
        "pattern": r"^\{CET\}\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
        "category": "timestamp_beacon",
        "extra_tags": ["beacon"],
    },
    # ── WX beacons ──────────────────────────────────────────────────────
    {
        "priority": 20,
        "name": "WX emoji block",
        "scope": "msg",
        "pattern": "🌡.*(📊|💧)",
        "category": "wx_beacon",
        "extra_tags": ["beacon", "emoji_heavy"],
    },
    {
        "priority": 21,
        "name": "WX text",
        "scope": "msg",
        "pattern": r"(?is)(WX\s|Temp[:= ]).*(QNH|hPa|mbar)",
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 22,
        "name": "WX short emoji",
        "scope": "msg",
        "pattern": r"🌡️\s*\d+",
        "category": "wx_beacon",
        "extra_tags": ["beacon", "emoji_heavy"],
    },
    {
        "priority": 23,
        "name": "WX pipe-delimited",
        "scope": "msg",
        "pattern": r"(?i)\bT:\s*-?\d+.*\|.*\bP:\s*\d+(\.\d+)?\s*(mb|hpa)\b",
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 24,
        "name": "WX Italian",
        "scope": "msg",
        "pattern": (
            r"(?is)\b(meteo|temp(?:eratura)?)\b"
            r".*\b(umid(?:it[àa])?|pioggia|pressione|vento)\b"
        ),
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 25,
        "name": "WX key=value",
        "scope": "msg",
        "pattern": (
            r"(?i)\bT\s*=\s*-?\d+(\.\d+)?\s*C?\b"
            r".*\bH\s*=\s*\d+%?\b"
            r".*\bP\s*=\s*\d+(\.\d+)?\s*(hpa|mb)\b"
        ),
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 26,
        "name": "WX APRS/CWOP",
        "scope": "msg",
        "pattern": (
            r"(?i)\b(APRS|CWOP)[ -/]?wetter\b"
            r"|\bCWOP\b.*\b(temperatur|luftdruck|feuchtigkeit)\b"
        ),
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 27,
        "name": "WX pollen",
        "scope": "msg",
        "pattern": r"(?i)\bpollen(wetter|vorhersage)\b",
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        # Matches DWD pollen beacons: "Roggen: keine [2/2]", "Gräser: gering",
        # "Birke: mittel bis hoch". The generic "WX pollen" rule only matches
        # the word "pollenwetter/pollenvorhersage", not these species:level
        # lines, so they previously fell through to "other".
        "priority": 27,
        "name": "WX pollen species",
        "scope": "msg",
        "pattern": (
            r"(?i)\b(Roggen|Gr(ä|ae)ser|Beifu(ß|ss)|Ambrosia|Birke|Erle|"
            r"Esche|Hasel|Ulme|Pappel|Weide|Eiche|Linde)\s*:\s*"
            r"(keine|gering|mittel|hoch)(\s+bis\s+(gering|mittel|hoch))?\b"
        ),
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        # Example: 🌙WX nördl.🌲¼ ▫️Temp.: 8.3 °C ▫️Feuchte: 57 % ▫️Wind: 3 km/h
        # No hPa/QNH — uses Feuchte/Regen/Wind/Solar instead.
        "priority": 28,
        "name": "WX German no-hPa",
        "scope": "msg",
        "pattern": r"(?i)\bWX\b.*\bTemp\.?:\s*-?\d+.*\b(Feuchte|Regen|Wind|Solar)\b",
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        # Example: JO44qp 1,3 Grad C, Wind NE 5 km/h, 1028 hPa. QAM 73, Timo
        # Locator-style manual WX reports with Grad C/F + hPa.
        "priority": 29,
        "name": "WX Grad hPa",
        "scope": "msg",
        "pattern": r"(?i)\b\d+[,.]?\d*\s*Grad\s*[CF]\b.*\bhPa\b",
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        # Matches any message containing Beaufort scale (BFT) — always weather.
        # E.g.: "Moin. wo in Kiel: 10,5 Grad, Ostwind 3-4 BFT, 8/8 sonnig"
        "priority": 29,
        "name": "WX Beaufort",
        "scope": "msg",
        "pattern": r"(?i)\bBFT\b",
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 29,
        "name": "WX Daten",
        "scope": "msg",
        "pattern": r"(?i)\bWX[_-]?Daten\b",
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
    },
    # ── SW / node adverts ───────────────────────────────────────────────
    {
        "priority": 30,
        "name": "MeshCom WebDesk version",
        "scope": "msg",
        "pattern": r"(?i)MeshComWebDesk v\d",
        "category": "sw_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 31,
        "name": "MeshCom WebDesk banner",
        "scope": "msg",
        "pattern": r"\*\*\*MeshCom WebDesk",
        "category": "sw_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 32,
        "name": "MeshCom/WebDesk mention",
        "scope": "msg",
        "pattern": r"(?i)\b(meshcom\s*webdesk|webdesk)\b",
        "category": "sw_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 33,
        "name": "FW version 73",
        "scope": "msg",
        "pattern": r"(?i)^\s*\d+\.\d+(\.\d+)?\s+73\s+de\s+[a-z0-9]+",
        "category": "sw_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 34,
        "name": "MeshDash beacon",
        "scope": "msg",
        "pattern": r"(?i)^\s*meshdash[\s,]",
        "category": "node_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 34,
        "name": "Telemetry CNT/INT",
        "scope": "msg",
        "pattern": r"^\s*\[CNT:\d+/\d+\]\s*\[INT:\d+\]",
        "category": "node_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 35,
        "name": "Contest termine",
        "scope": "msg",
        "pattern": r"(?i)\bcontest\s*termine\b",
        "category": "node_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 38,
        "name": "Pong reply",
        "scope": "msg",
        "pattern": r"(?i)(^pong[!.\s]|↩️\s*pong\b)",
        "category": "node_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 39,
        "name": "MH heard list",
        "scope": "msg",
        "pattern": r"^\s*MH\(\d+\):\s*[A-Z0-9]+-\d+",
        "category": "node_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 36,
        "name": "HTML advert",
        "scope": "msg",
        "pattern": r"<(div|a|span|p|br)\b[^>]*>",
        "category": "node_advert",
        "extra_tags": ["beacon", "has_url"],
    },
    {
        "priority": 37,
        "name": "Emoji URL advert",
        "scope": "msg",
        "pattern": (
            r"(?s)[\U0001F300-\U0001FAFF\u2600-\u27BF\u2300-\u23FF]"
            r".*\b(https?://|www\.)\S+"
        ),
        "category": "node_advert",
        "extra_tags": ["beacon", "has_url"],
    },
    {
        "priority": 40,
        "name": "QTH beacon",
        "scope": "msg",
        "pattern": r"(?i)^\s*QTH[\s:|]",
        "category": "node_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 40,
        "name": "Route beacon",
        "scope": "msg",
        "pattern": r"(?i)^\s*Route:\s*[A-Z0-9]+-\d+",
        "category": "node_advert",
        "extra_tags": ["beacon"],
    },
    {
        "priority": 41,
        "name": "Banner advert",
        "scope": "msg",
        "pattern": r"^\s*-{2,}=+.*=+-{2,}\s*$",
        "category": "node_advert",
        "extra_tags": ["beacon"],
    },
    # priority 42 — broadens the original "URL advert" (was https?:// only, priority 40)
    {
        "priority": 42,
        "name": "URL advert",
        "scope": "msg",
        "pattern": r"^\s*(https?://|www\.)[^\s]+\s*$",
        "category": "node_advert",
        "extra_tags": ["has_url"],
    },
    # ── Test messages ───────────────────────────────────────────────────
    {
        # "test", "prova" (IT), "probe" (DE), "essai" (FR) at message start.
        # Optional leading emoji/symbols (e.g. "👋 test 1-2-3").
        # Priority before greeting so "test ciao" → test_msg, not greeting.
        "priority": 48,
        "name": "Test message",
        "scope": "msg",
        "pattern": r"(?i)^[^\w]*(test|prova|probe|proberuf|versuch|essai|prueba)(\W|$)",
        "category": "test_msg",
        "extra_tags": ["test"],
    },
    # ── Greetings / alerts / directed ──────────────────────────────────
    {
        # Optional leading emoji/symbols before the greeting word so that
        # "👋😊 Buon pomeriggio" still matches despite the ^ anchor.
        # Italian additions: buon pomeriggio, buonanotte, buona notte, buonasera.
        # German additions: gute nacht, gute reise.
        # English additions: good morning, good evening, good night, good afternoon, good day.
        "priority": 50,
        "name": "Greeting DE/EN",
        "scope": "msg",
        "pattern": (
            r"(?i)^[^\w]*("
            r"73|hallo|servus|moin|nabend|ahoi|"
            r"guten\s+(morgen|abend|tag)|gute\s+(nacht|reise)|"
            r"good\s+(morning|evening|night|afternoon|day)|"
            r"buon ?giorno|buona?s?era|buona ?notte|buonanotte|"
            r"buon ?pomeriggio|ciao|hello|hi\b|qsl[?!]?"
            r")"
        ),
        "category": "greeting",
        "extra_tags": [],
    },
    {
        "priority": 60,
        "name": "Earthquake alert",
        "scope": "msg",
        "pattern": r"(?i)(erdbeben|earthquake|magnitude\s+\d)",
        "category": "alert",
        "extra_tags": [],
    },
    {
        "priority": 55,
        "name": "Alert reminder",
        "scope": "msg",
        "pattern": r"(?i)⚠[️]?\s*erinnerung|erinnerung\s*⚠",
        "category": "alert",
        "extra_tags": [],
    },
    {
        "priority": 90,
        "name": "Direct callsign",
        "scope": "dst",
        "pattern": r"^[A-Z0-9]+-\d+$",
        "category": "directed",
        "extra_tags": [],
    },
]


def _needs_update(existing: dict[str, Any], default: dict[str, Any]) -> bool:
    """True if any of pattern/category/priority/scope/extra_tags drifted."""
    if existing.get("pattern") != default["pattern"]:
        return True
    if existing.get("category") != default["category"]:
        return True
    if existing.get("priority") != default["priority"]:
        return True
    if existing.get("scope") != default["scope"]:
        return True
    existing_tags = tuple(existing.get("extra_tags") or ())
    default_tags = tuple(default.get("extra_tags") or ())
    return existing_tags != default_tags


async def seed_defaults(storage: StorageProtocol) -> tuple[int, int]:
    """Upsert DEFAULT_RULES by name.

    Returns ``(inserted, updated)``.  User-created rules (``builtin=0``)
    are never modified — only builtin rows that have drifted from
    ``DEFAULT_RULES`` are updated in place.
    """
    existing = await storage.get_classifier_rules()
    by_name: dict[str, dict[str, Any]] = {r["name"]: r for r in existing}

    inserted = 0
    updated = 0
    for rule in DEFAULT_RULES:
        row = by_name.get(rule["name"])
        if row is None:
            await storage.insert_classifier_rule(
                name=rule["name"],
                pattern=rule["pattern"],
                scope=rule["scope"],
                category=rule["category"],
                extra_tags=rule["extra_tags"] or None,
                priority=rule["priority"],
                enabled=True,
                builtin=True,
            )
            inserted += 1
            continue

        if not row.get("builtin"):
            # User re-used a builtin name — leave it alone.
            continue

        if _needs_update(row, rule):
            await storage.update_classifier_rule(
                row["id"],
                pattern=rule["pattern"],
                scope=rule["scope"],
                category=rule["category"],
                extra_tags=rule["extra_tags"] or None,
                priority=rule["priority"],
            )
            updated += 1

    return inserted, updated
