"""Deduplizierung (Abschnitt 8).

Primaer: (Plattform, Plattform-Listing-ID).
Fallback: kanonische URL, danach (Plattform, normalisierter Titel,
abgerundeter Gesamtpreis, Verkäufer).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .normalize import compact_key, normalize_text

# Tracking-Parameter, die die URL nicht inhaltlich verändern.
_TRACKING_PREFIXES = ("utm_", "mpre", "campid", "toolid", "customid", "hash", "amdata")
_TRACKING_KEYS = {"_trksid", "_trkparms", "epid", "chn", "geo_id", "loc", "nordt", "rt"}


def canonical_url(url: str | None) -> str:
    """Entfernt Tracking-Parameter und normalisiert Schema/Host."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:  # pragma: no cover - defensiv
        return normalize_text(url)
    if not parsed.scheme and not parsed.netloc:
        return normalize_text(url)

    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_KEYS and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
    cleaned = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=path,
        query=urlencode(query_pairs),
        fragment="",
    )
    return urlunparse(cleaned)


def fingerprint(
    platform: str,
    title: str | None,
    total_price: float | None,
    seller: str | None,
) -> str:
    """Inhaltlicher Fingerabdruck für die Fallback-Deduplizierung.

    Der Preis wird auf ganze Euro gerundet, damit minimale Rundungs-
    unterschiede zwischen Suchläufen keine Duplikate erzeugen.
    """
    price_bucket = int(round(total_price)) if total_price is not None else -1
    return "|".join(
        (
            normalize_text(platform),
            compact_key(title),
            str(price_bucket),
            normalize_text(seller),
        )
    )
