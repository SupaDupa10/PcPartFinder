"""Normalisierung von Angebotsdaten.

Alle Funktionen hier sind deterministisch und ohne Netzwerkzugriff - sie
bilden die verlaessliche Basis, auf die sich Suchlauf und Scoring stuetzen.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

_UMLAUT_MAP = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "Ä": "ae",
        "Ö": "oe",
        "Ü": "ue",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")

# Rauschbegriffe, die in Titeln haeufig vorkommen und die Modellerkennung
# stoeren. Sie werden fuer den Suchvergleich entfernt, nicht fuer die Anzeige.
_NOISE_TOKENS = frozenset(
    {
        "neu",
        "new",
        "ovp",
        "originalverpackt",
        "gebraucht",
        "used",
        "defekt",
        "bastler",
        "top",
        "wie",
        "inkl",
        "inklusive",
        "mit",
        "ohne",
        "und",
        "der",
        "die",
        "das",
        "fuer",
        "für",
        "von",
        "zum",
        "zur",
        "sehr",
        "gut",
        "original",
        "rechnung",
        "garantie",
        "versand",
        "gratis",
        "schnell",
    }
)


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(value: str | None) -> str:
    """Kleinschreibung, Umlaut-Aufloesung, Whitespace-Kollaps."""
    if not value:
        return ""
    text_value = value.translate(_UMLAUT_MAP)
    text_value = strip_accents(text_value)
    text_value = text_value.lower()
    text_value = _WHITESPACE_RE.sub(" ", text_value)
    return text_value.strip()


def tokenize(value: str | None, *, drop_noise: bool = False) -> list[str]:
    text_value = normalize_text(value)
    tokens = [t for t in _NON_WORD_RE.split(text_value) if t]
    if drop_noise:
        tokens = [t for t in tokens if t not in _NOISE_TOKENS]
    return tokens


def compact_key(value: str | None) -> str:
    """Alphanumerischer Schluessel fuer robuste Modellvergleiche.

    'RTX 3080 10GB' -> 'rtx308010gb', 'rtx-3080' -> 'rtx3080'
    """
    return "".join(tokenize(value))


TOKEN_DELIM = "\x1f"


def token_key(value: str | None) -> str:
    """Tokenweise, delimitergetrennte Schluesselform fuer praezise Wortfolgen."""
    return TOKEN_DELIM.join(tokenize(value))


def match_key_contains(haystack: str | None, needle: str | None, *, min_len: int = 4) -> bool:
    """Prueft, ob ``needle`` als zusammenhaengende Wortfolge in ``haystack`` steht.

    Zwei Stufen, weil Hardwarebezeichnungen in Anzeigen sehr uneinheitlich
    geschrieben werden:

    1. Tokenweise (delimitergetrennt) - praezise: 'rtx 3080' matcht
       'ASUS RTX 3080 Ti OC', aber nicht 'RTX 30800'.
    2. Kompaktiert als Rueckfall - matcht auch 'RTX3080' in 'RTX3080Ti'.
       Ein direkt folgendes Ziffernzeichen wird abgelehnt, damit 'rx580'
       nicht faelschlich in 'rx5800' gefunden wird.

    Kurze Begriffe (< ``min_len`` Zeichen) werden bewusst ignoriert, um
    Rauschen wie 'rx' oder 'i5' zu vermeiden.
    """
    needle_tokens = tokenize(needle)
    if not needle_tokens:
        return False
    hay_tokens = tokenize(haystack)
    if not hay_tokens:
        return False

    needle_compact = "".join(needle_tokens)
    if len(needle_compact) < min_len:
        return False

    # 1a. Der Suchbegriff steht als exaktes Einzelwort im Text ('rx580').
    if needle_compact in hay_tokens:
        return True

    # 1b. Der Suchbegriff steht als zusammenhaengende Wortfolge im Text
    #     ('rtx 3080' in 'ASUS RTX 3080 Ti OC').
    delimited_hay = TOKEN_DELIM + TOKEN_DELIM.join(hay_tokens) + TOKEN_DELIM
    delimited_needle = TOKEN_DELIM + TOKEN_DELIM.join(needle_tokens) + TOKEN_DELIM
    if delimited_needle in delimited_hay:
        return True

    hay_compact = "".join(hay_tokens)
    start = 0
    while True:
        index = hay_compact.find(needle_compact, start)
        if index == -1:
            return False
        end = index + len(needle_compact)
        if end >= len(hay_compact) or not hay_compact[end].isdigit():
            return True
        start = index + 1


# ---------------------------------------------------------------------------
# Verneinungen
# ---------------------------------------------------------------------------

# Verneinungen sind im Gebrauchtmarkt allgegenwaertig ("kein Spulenfiepen",
# "ohne Garantie", "nicht defekt"). Ohne Behandlung entstehen daraus falsche
# Signale - im schlimmsten Fall gilt ein Text als "defekt", weil er einen
# Defekt ausdruecklich verneint.
NEGATION_TOKENS = frozenset(
    {
        "nicht",
        "kein",
        "keine",
        "keinen",
        "keinem",
        "keiner",
        "keines",
        "keinerlei",
        "ohne",
        "nichts",
        "nie",
        "niemals",
        "no",
        "not",
        "without",
    }
)

_SENTENCE_SPLIT_RE = re.compile(r"[.,;:!?\n\-–—/]")
# Nur der direkte Umkreis des Treffers zaehlt, damit eine Verneinung am
# Satzanfang nicht weit entfernte Begriffe entwerten kann.
_NEGATION_WINDOW_CHARS = 30
_NEGATION_LOOKBACK_TOKENS = 3
# Verneinungen NACH dem Muster sind heikler: "defekt ohne Zubehoer" ist ein
# Defekt, "Rechnung nicht dabei" ist keine Rechnung. Deshalb ein engerer
# Umkreis und ohne "ohne", das nach einem Signal meist etwas anderes meint.
_TRAILING_NEGATION_LOOKBACK_TOKENS = 2
_TRAILING_NEGATION_TOKENS = NEGATION_TOKENS - {"ohne"}


def _has_negation(tokens: list[str]) -> bool:
    return any(token in NEGATION_TOKENS for token in tokens)


def _looks_negated(normalized_text: str, index: int, length: int, *, trailing: bool) -> bool:
    """Steht unmittelbar vor (und optional direkt nach) der Fundstelle eine Verneinung?

    Beide Richtungen sind noetig:

    * "kein Spulenfiepen" -> Verneinung VOR dem Muster.
    * "funktioniert nicht" -> Verneinung NACH dem Muster.

    Die Verneinung wirkt nur innerhalb desselben Satzteils, damit ein "kein"
    am Satzanfang nicht spaeter genannte Signale entwertet.
    """
    start = max(0, index - _NEGATION_WINDOW_CHARS)
    before = _SENTENCE_SPLIT_RE.split(normalized_text[start:index])[-1]
    before_tokens = [t for t in _NON_WORD_RE.split(before) if t]
    if _has_negation(before_tokens[-_NEGATION_LOOKBACK_TOKENS:]):
        return True
    if not trailing:
        return False
    end = index + length
    after = _SENTENCE_SPLIT_RE.split(normalized_text[end : end + _NEGATION_WINDOW_CHARS])[0]
    after_tokens = [t for t in _NON_WORD_RE.split(after) if t]
    return any(
        token in _TRAILING_NEGATION_TOKENS
        for token in after_tokens[:_TRAILING_NEGATION_LOOKBACK_TOKENS]
    )


def pattern_present(
    haystack: str | None,
    pattern: str | None,
    *,
    respect_negation: bool = True,
    trailing_negation: bool = True,
    min_token_len: int = 4,
) -> bool:
    """Prueft ein Muster im normalisierten Text und respektiert Verneinungen.

    Wird von der Zustandserkennung UND von der Risikoanalyse benutzt, damit
    beide Stellen dieselbe, nachvollziehbare Regel verwenden.

    Kurze, rein alphanumerische Muster werden nur als vollstaendiges Wort
    akzeptiert ('oc' nicht in 'block', 'neu' nicht in 'neuwertig' == doch,
    siehe unten: 'neuwertig' wird von einem eigenen Muster erfasst).
    """
    normalized = normalize_text(haystack)
    needle = normalize_text(pattern)
    if not normalized or not needle:
        return False

    tokens = tokenize(normalized)
    is_word_only = needle.isalnum() and len(needle) <= min_token_len
    if is_word_only and needle not in tokens:
        return False

    start = 0
    while True:
        index = normalized.find(needle, start)
        if index < 0:
            return False
        if not respect_negation or not _looks_negated(
            normalized, index, len(needle), trailing=trailing_negation
        ):
            return True
        start = index + 1


def content_hash(*parts: str | None) -> str:
    """Stabiler Hash ueber die inhaltlich relevanten Felder eines Angebots.

    Dient dazu, unveraenderte Angebote nicht erneut zu bewerten.
    """
    joined = "\u0001".join(normalize_text(p) for p in parts if p)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Preise
# ---------------------------------------------------------------------------

# Erkennt Formate wie "1.234,56", "1,234.56", "250", "250,00", "250 €", "EUR 250"
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def parse_price(value: object) -> float | None:
    """Parst Preise aus Zahlen und aus deutschen/englischen Textformaten."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if number >= 0 else None
    if not isinstance(value, str):
        return None

    match = _NUMBER_RE.search(value)
    if not match:
        return None
    raw = match.group(0)
    try:
        return _decimal_from_localized(raw)
    except InvalidOperation:  # pragma: no cover - defensiv
        return None


def _decimal_from_localized(raw: str) -> float:
    """Interpretiert '1.234,56' als 1234.56 und '1,234.56' als 1234.56."""
    if "," in raw and "." in raw:
        # Das zuletzt auftretende Trennzeichen ist das Dezimaltrennzeichen.
        if raw.rfind(",") > raw.rfind("."):
            cleaned = raw.replace(".", "").replace(",", ".")
        else:
            cleaned = raw.replace(",", "")
    elif "," in raw:
        # Mehrere Kommas -> Tausendertrenner (z. B. '1,234,567')
        cleaned = raw.replace(",", ".") if raw.count(",") == 1 else raw.replace(",", "")
    else:
        # Einzelner Punkt oder gar keiner: '250.00' (Dezimal) vs. '1.234' (Tausender).
        # Mehrere Punkte sind immer Tausendertrenner.
        # Ein einzelner Punkt mit genau drei Nachkommastellen wird im deutschen
        # Gebrauchtmarkt-Kontext als Tausendertrenner gewertet ('1.234' -> 1234),
        # alles andere als Dezimaltrennzeichen ('250.00' -> 250.0).
        if raw.count(".") > 1:
            cleaned = raw.replace(".", "")
        elif raw.count(".") == 1:
            head, _, tail = raw.partition(".")
            cleaned = raw.replace(".", "") if len(tail) == 3 else raw
        else:
            cleaned = raw
    return float(Decimal(cleaned))


def round_money(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value) + 1e-9, 2)


def compute_total_price(
    price: float | None,
    shipping_cost: float | None,
    shipping_known: bool,
    pickup_available: bool = False,
) -> float | None:
    """Gesamtpreis.

    Wichtig: Ist die Versandkostenhoehe unbekannt und ist keine Abholung
    moeglich, wird NICHT einfach 0 addiert - der Gesamtpreis bleibt dann
    unsicher und wir geben den bekannten Artikelpreis zurueck, waehrend
    ``shipping_known`` False bleibt und das Scoring dies abwertet.
    """
    if price is None:
        return None
    if shipping_known and shipping_cost is not None:
        return round_money(price + shipping_cost)
    return round_money(price)


# ---------------------------------------------------------------------------
# Zustand
# ---------------------------------------------------------------------------

CONDITION_UNKNOWN = -1
CONDITION_DEFECT = 0
CONDITION_ACCEPTABLE = 1
CONDITION_GOOD = 2
CONDITION_VERY_GOOD = 3
CONDITION_NEW = 4

CONDITION_LABELS = {
    CONDITION_DEFECT: "Defekt / Ersatzteil",
    CONDITION_ACCEPTABLE: "Akzeptabel",
    CONDITION_GOOD: "Gebraucht, gut",
    CONDITION_VERY_GOOD: "Sehr gut / wie neu",
    CONDITION_NEW: "Neu / ungeöffnet",
    CONDITION_UNKNOWN: "Unbekannt",
}

# Zustände, die eine Gebraucht-Suche zulässt: gebraucht, aber nicht defekt und
# nicht neu. Die Ingestion verwirft Defekt und Neuware ohnehin (siehe
# ``search.ingest.defect_or_new_reason``) - sie schon auf der Quelle
# auszuschließen spart Anfragen und Seiten: eBay liefert sonst zu jeder
# Komponente überwiegend Neuware, die nie gespeichert würde.
SEARCHABLE_CONDITIONS: tuple[int, ...] = (
    CONDITION_ACCEPTABLE,
    CONDITION_GOOD,
    CONDITION_VERY_GOOD,
)


def searchable_condition_ranks(min_rank: int | None = None) -> list[int]:
    """Zustände der Suche als Rangfolge, optional ab einem Mindestrang.

    ``min_rank`` ist die Einstellung aus der Oberfläche. Werte unter
    ``CONDITION_ACCEPTABLE`` werden angehoben (Defekt wird nie gesucht),
    Werte ab ``CONDITION_NEW`` werden gesenkt (Neuware wird nie gespeichert).
    """
    floor = CONDITION_ACCEPTABLE
    if min_rank is not None:
        floor = min(max(int(min_rank), CONDITION_ACCEPTABLE), CONDITION_VERY_GOOD)
    return [rank for rank in SEARCHABLE_CONDITIONS if rank >= floor]

# Reihenfolge ist wichtig: spezifischere Muster zuerst.
_CONDITION_RULES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        CONDITION_DEFECT,
        (
            "for parts",
            "not working",
            "als ersatzteil",
            "ersatzteil",
            "defekt",
            "kaputt",
            "bastler",
            "broken",
            "faulty",
            "nicht funktionsfaehig",
            "nicht funktionsfähig",
            "displaybruch",
        ),
    ),
    (
        CONDITION_ACCEPTABLE,
        ("akzeptabel", "acceptable", "used acceptable", "starke gebrauchsspuren", "abgenutzt"),
    ),
    (
        CONDITION_GOOD,
        (
            "gebraucht, gut",
            "gut - refurbished",
            "good",
            "gebrauchsspuren",
            "used",
            "gebraucht",
            "second hand",
        ),
    ),
    (
        CONDITION_VERY_GOOD,
        (
            "sehr gut - refurbished",
            "refurbished",
            "generalueberholt",
            "generalüberholt",
            "sehr gut",
            "very good",
            "excellent",
            "hervorragend",
            "wie neu",
            "kaum gebraucht",
            "neuwertig",
        ),
    ),
    (
        CONDITION_NEW,
        (
            "neu mit ovp",
            "neu: sonstige",
            "neu ohne etikett",
            "neu mit etikett",
            "neu, ovp",
            "originalverpackt",
            "versiegelt",
            "sealed",
            "brand new",
            "new in box",
            "brandneu",
            "neu",
            "new",
        ),
    ),
)

# eBay conditionId -> Rang. Nur als Rueckfall, wenn der Text nicht greift.
# Quelle: oeffentliche eBay-Condition-IDs (Buy/Browse API).
_CONDITION_ID_RANGES: tuple[tuple[int, int, int], ...] = (
    (1000, 1499, CONDITION_NEW),
    (1500, 1749, CONDITION_NEW),
    (1750, 1999, CONDITION_ACCEPTABLE),
    (2000, 2999, CONDITION_VERY_GOOD),
    (3000, 3999, CONDITION_GOOD),
    (4000, 4999, CONDITION_VERY_GOOD),
    (5000, 5999, CONDITION_GOOD),
    (6000, 6999, CONDITION_ACCEPTABLE),
    (7000, 7999, CONDITION_DEFECT),
)


# Muster, die nur die Nutzungsklasse beschreiben ("gebraucht") und deshalb
# niemals eine ausdrueckliche Qualitaetsnote ueberschreiben duerfen.
# "Gebraucht, sehr gut" bedeutet Rang 3, nicht Rang 2.
_GENERIC_CONDITION_PATTERNS = frozenset(
    {"gebraucht", "used", "second hand", "gebrauchsspuren"}
)


def condition_rank_from_text(text_value: str | None) -> int | None:
    """Leitet einen Zustandsrang aus Freitext ab. ``None`` = nicht bestimmbar.

    Bewertungsregeln, in dieser Reihenfolge:

    1. Verneinte Aussagen zaehlen nicht ("nicht defekt", "ohne Kratzer").
    2. Spezifische Qualitaetsangaben schlagen generische Angaben: das laengste
       passende Muster gewinnt, damit "Sehr gut - Refurbished" nicht von
       "Gut - Refurbished" ueberstimmt wird.
    3. "Gebraucht"/"used" allein ist nur ein Rueckfall, wenn keine
       Qualitaetsangabe vorhanden ist.
    """
    normalized = normalize_text(text_value)
    if not normalized:
        return None

    matches: list[tuple[int, str, bool]] = []
    for rank, patterns in _CONDITION_RULES:
        for pattern in patterns:
            if pattern_present(normalized, pattern):
                matches.append((rank, pattern, pattern in _GENERIC_CONDITION_PATTERNS))
    if not matches:
        return None

    specific = [item for item in matches if not item[2]]
    pool = specific or matches
    pool.sort(key=lambda item: len(item[1]), reverse=True)
    return pool[0][0]


def condition_rank_from_id(condition_id: object) -> int | None:
    try:
        numeric = int(str(condition_id).strip())
    except (TypeError, ValueError):
        return None
    for low, high, rank in _CONDITION_ID_RANGES:
        if low <= numeric <= high:
            return rank
    return None


def resolve_condition_rank(
    condition_text: str | None, condition_id: object | None = None
) -> int | None:
    """Text hat Vorrang (sprachunabhaengig verifizierbar), ID ist Rueckfall."""
    rank = condition_rank_from_text(condition_text)
    if rank is not None:
        return rank
    if condition_id is not None:
        return condition_rank_from_id(condition_id)
    return None


# ---------------------------------------------------------------------------
# Zeit
# ---------------------------------------------------------------------------


def parse_datetime(value: object) -> datetime | None:
    """Parst ISO-8601 (auch mit 'Z') und Unix-Zeitstempel. Sonst ``None``.

    Ein ``None`` bedeutet ausdruecklich "Zeitpunkt unbekannt" und wird im
    Scoring nicht als aktuelles Angebot behandelt.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # Millisekunden
            seconds /= 1000.0
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return None
        if text_value.endswith(("Z", "z")):
            text_value = text_value[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text_value)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_days(published_at: datetime | None, now: datetime | None = None) -> float | None:
    if published_at is None:
        return None
    reference = now or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return max(0.0, (reference - published_at).total_seconds() / 86400.0)


# ---------------------------------------------------------------------------
# Standort
# ---------------------------------------------------------------------------


def normalize_postal_code(value: object) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 5:
        return digits
    if len(digits) == 4:  # oesterreichische/schweizer PLZ
        return digits
    return None


def postcode_distance_km(a: str | None, b: str | None) -> float | None:
    """Grobe Entfernungsschaetzung zwischen zwei PLZ.

    Es wird bewusst KEINE exakte Entfernung erfunden: Ohne belastbare
    Geodaten liefert die Funktion ``None``. Die hier genutzte Naeherung
    beruht auf der PLZ-Zonenstruktur in Deutschland (erste Ziffer) und
    wird in der UI als "grob" gekennzeichnet.
    """
    if not a or not b or len(a) < 2 or len(b) < 2:
        return None
    try:
        zone_a = int(a[0])
        zone_b = int(b[0])
    except ValueError:
        return None
    # Deutsche PLZ-Zonen verlaufen grob von Nord (0) nach Sued (9).
    # ~110 km pro Zonensprung ist eine konservative Mittelung.
    return float(abs(zone_a - zone_b) * 110)
