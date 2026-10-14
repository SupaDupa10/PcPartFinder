"""Tests fuer Auktionsausschluss, Formatklassifizierung und Deduplizierung."""

from __future__ import annotations

import pytest

from backend.domain.buying import (
    FORMAT_AUCTION,
    FORMAT_FIXED_PRICE,
    FORMAT_UNKNOWN,
    classify_format,
    is_acceptable,
)
from backend.domain.dedup import canonical_url, fingerprint


class TestFormatClassification:
    def test_festpreis_wird_akzeptiert(self):
        decision = classify_format(buying_options=["FIXED_PRICE"], fixed_price=199.0)
        assert decision.format == FORMAT_FIXED_PRICE
        assert decision.fixed_price_available is True
        ok, _ = is_acceptable(decision)
        assert ok is True

    def test_auktion_wird_ausgeschlossen(self):
        decision = classify_format(
            buying_options=["AUCTION"], current_bid=150.0, bid_count=12
        )
        assert decision.format == FORMAT_AUCTION
        ok, reason = is_acceptable(decision)
        assert ok is False
        assert "Auktion" in (reason or "")

    def test_gebotszahl_allein_reicht_zum_ausschluss(self):
        """Ohne strukturierte Angaben darf ein Gebot nicht als Festpreis gelten."""
        decision = classify_format(buying_options=None, bid_count=3, current_bid=90.0)
        assert decision.format == FORMAT_AUCTION
        ok, _ = is_acceptable(decision)
        assert ok is False

    def test_gemischtes_format_mit_separatem_sofortkauf(self):
        decision = classify_format(
            buying_options=["AUCTION", "FIXED_PRICE"],
            fixed_price=280.0,
            current_bid=190.0,
            bid_count=2,
        )
        assert decision.format == FORMAT_FIXED_PRICE
        assert decision.mixed is True
        assert decision.fixed_price_available is True
        ok, _ = is_acceptable(decision, allow_mixed=True)
        assert ok is True
        ok_strict, reason = is_acceptable(decision, allow_mixed=False)
        assert ok_strict is False
        assert "Sofort-Kauf" in (reason or "")

    def test_gemischtes_format_ohne_separaten_festpreis_wird_ausgeschlossen(self):
        """Wenn der Preis nur das aktuelle Gebot ist, ist er kein Festpreis."""
        decision = classify_format(
            buying_options=["AUCTION", "FIXED_PRICE"], fixed_price=150.0, current_bid=150.0
        )
        assert decision.format == FORMAT_UNKNOWN
        ok, _ = is_acceptable(decision)
        assert ok is False

    def test_unbekanntes_format_wird_standardmaessig_ausgeschlossen(self):
        decision = classify_format()
        assert decision.format == FORMAT_UNKNOWN
        ok, reason = is_acceptable(decision)
        assert ok is False
        assert "unbekannt" in (reason or "").lower()

    def test_bekannte_aber_nicht_zulaessige_option_ist_kein_festpreis(self):
        """Beide Optionen zusammen ohne Preiswert bleiben unbekannt."""
        decision = classify_format(buying_options=["BEST_OFFER"], fixed_price=50.0)
        assert decision.format == FORMAT_UNKNOWN

    def test_connector_format_als_rueckfall(self):
        decision = classify_format(platform_format="fixed_price", fixed_price=120.0)
        assert decision.format == FORMAT_FIXED_PRICE
        auction = classify_format(platform_format="auction")
        assert auction.format == FORMAT_AUCTION


class TestDedup:
    def test_url_normalisierung_entfernt_tracking(self):
        assert canonical_url("https://example.invalid/a/1?utm_source=x#frag") == canonical_url(
            "https://example.invalid/a/1"
        )
        # Leere/fehlende URLs liefern einen leeren Schluessel, kein None.
        assert canonical_url("") == ""
        assert canonical_url(None) == ""

    def test_fingerprint_ist_stabil_und_unabhaengig_von_formatierung(self):
        first = fingerprint("local_import", "RTX  3080 Grafikkarte", 210.0, "techsammler")
        second = fingerprint("local_import", "rtx 3080 grafikkarte", 210.0, "TECHSAMMLER")
        assert first == second
        assert fingerprint("ebay", "RTX 3080", 210.0, "techsammler") != first

    def test_fingerprint_ohne_preis_ist_trotzdem_stabil(self):
        assert fingerprint("local_import", "Titel", None, None).endswith("|-1|")
        assert fingerprint("local_import", "Titel", None, None) == fingerprint(
            "local_import", "Titel", None, None
        )
