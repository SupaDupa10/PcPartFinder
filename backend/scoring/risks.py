"""Deterministische Risiko- und Qualitaetssignale aus Anzeigentexten.

Diese Analyse ist vollstaendig deterministisch und laeuft immer.
Sie liefert die Signale und die Erklaerung
verbessern, aber der Basisscore haengt nie allein an KI-Ausgaben.

Alle Signale sind nachvollziehbar: Jedes liefert einen Code, eine deutsche
Begruendung und eine Schwere (0..1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.normalize import normalize_text, pattern_present, tokenize


@dataclass(frozen=True)
class RiskSignal:
    code: str
    label: str
    severity: float  # 0..1


@dataclass
class TextRiskFindings:
    negatives: list[RiskSignal] = field(default_factory=list)
    positives: list[RiskSignal] = field(default_factory=list)

    @property
    def risk_score(self) -> float:
        """Aggregierte Risikolast 0..1 (1 = maximal riskant)."""
        if not self.negatives:
            return 0.0
        # Staerkstes Signal dominiert, weitere Signale ergaenzen gedaempft.
        ordered = sorted((n.severity for n in self.negatives), reverse=True)
        total = ordered[0] + sum(s * 0.35 for s in ordered[1:])
        return min(1.0, total)

    @property
    def positive_score(self) -> float:
        if not self.positives:
            return 0.0
        ordered = sorted((p.severity for p in self.positives), reverse=True)
        return min(1.0, ordered[0] + sum(s * 0.25 for s in ordered[1:]))

    def labels(self) -> list[str]:
        return [n.label for n in self.negatives]

    def to_dict(self) -> dict:
        return {
            "risk_score": round(self.risk_score, 3),
            "positive_score": round(self.positive_score, 3),
            "negatives": [n.__dict__ for n in self.negatives],
            "positives": [p.__dict__ for p in self.positives],
        }


# (Code, Suchmuster, deutsches Label, Schwere)
# Muster werden auf normalisierten Text (klein, ohne Umlaute) angewendet.
NEGATIVE_PATTERNS: tuple[tuple[str, str, str, float], ...] = (
    ("defect", "defekt", "Als defekt gekennzeichnet", 0.95),
    ("for_parts", "ersatzteil", "Wird als Ersatzteil angeboten", 0.95),
    ("bastler", "bastler", "Bastlerartikel", 0.85),
    ("not_working", "funktioniert nicht", "Funktion wird verneint", 0.95),
    ("not_functional", "nicht funktionsfaehig", "Nicht funktionsfähig", 0.95),
    ("does_not_run", "laeuft nicht", "Läuft nicht", 0.9),
    ("does_not_work", "geht nicht", "Funktioniert nicht", 0.85),
    ("does_not_start", "startet nicht", "Startet nicht", 0.9),
    ("no_display_output", "gibt kein bild", "Kein Bildsignal", 0.9),
    ("no_signal", "kein bildsignal", "Kein Bildsignal", 0.9),
    ("nicht_getestet", "nicht getestet", "Funktion nicht getestet", 0.6),
    ("untested", "ungeprueft", "Zustand nicht geprueft", 0.45),
    ("no_return", "keine ruecknahme", "Keine Rücknahme möglich", 0.35),
    ("no_warranty", "keine garantie", "Keine Garantie", 0.3),
    ("no_warranty2", "ohne garantie", "Keine Garantie", 0.3),
    ("no_receipt", "ohne rechnung", "Kein Kaufbeleg vorhanden", 0.4),
    ("no_receipt2", "kein kaufbeleg", "Kein Kaufbeleg vorhanden", 0.4),
    ("mining", "mining", "Möglicherweise Mining-Nutzung", 0.55),
    ("gaming_24_7", "24/7", "Dauerbetrieb möglich", 0.25),
    ("overclocked", "uebertaktet", "Übertaktet betrieben", 0.35),
    ("oc", " oc ", "Übertaktet (OC)", 0.2),
    ("water_damage", "wasserschaden", "Wasserschaden erwähnt", 0.9),
    ("bent_pins", "verbogene pins", "Verbogene Pins", 0.85),
    ("broken_pins", "abgebrochene pins", "Abgebrochene Pins", 0.9),
    ("no_accessories", "ohne zubehoer", "Zubehör fehlt", 0.4),
    ("no_accessories2", "zubehoer fehlt", "Zubehör fehlt", 0.4),
    ("no_accessories3", "fehlendes zubehoer", "Zubehör fehlt", 0.4),
    ("no_cable", "ohne kabel", "Kabel fehlen", 0.4),
    ("no_box", "ohne ovp", "Ohne Originalverpackung", 0.15),
    ("missing_screws", "schrauben fehlen", "Befestigungsmaterial fehlt", 0.2),
    ("fan_defect", "luefter defekt", "Luefter defekt", 0.7),
    ("coil_whine", "spulenfiepen", "Spulenfiepen vorhanden", 0.3),
    ("bios_lock", "bios gesperrt", "BIOS/System gesperrt", 0.6),
    ("bios_locked", "gesperrt", "Gerät möglicherweise gesperrt", 0.5),
    ("display_damage", "displaybruch", "Display beschaedigt", 0.9),
    ("cracked", "riss", "Riss/Schaden erwaehnt", 0.6),
    ("oxidized", "oxidiert", "Oxidation/Korrosion", 0.8),
    ("bent", "verbogen", "Verformung erwähnt", 0.7),
    ("scratched_heavy", "kratzer", "Sichtbare Kratzer", 0.2),
    ("no_psu_cable", "netzteil fehlt", "Netzteil fehlt", 0.35),
    ("unknown_model", "unbekannt", "Angaben unklar", 0.3),
    ("as_is", "wie gesehen", "Verkauf wie gesehen", 0.5),
    ("fake", "replika", "Möglicherweise Nachbau", 0.7),
)

POSITIVE_PATTERNS: tuple[tuple[str, str, str, float], ...] = (
    ("bill", "rechnung", "Kaufbeleg vorhanden", 0.7),
    ("warranty", "garantie", "Garantie angegeben", 0.7),
    ("original_box", "ovp", "Originalverpackung vorhanden", 0.5),
    ("tested", "getestet", "Als getestet beschrieben", 0.6),
    ("works", "funktioniert einwandfrei", "Funktion bestätigt", 0.8),
    ("sealed", "versiegelt", "Originalversiegelt", 0.8),
    ("refurbished", "refurbished", "Generalüberholt", 0.5),
    ("benchmark", "bechmark", "Mit Benchmark-Nachweis", 0.4),
    ("stress_test", "stresstest", "Mit Stresstest-Nachweis", 0.4),
    ("from_dealer", "haendler", "Händlerverkauf", 0.5),
    ("invoice_available", "kaufbeleg", "Kaufbeleg vorhanden", 0.7),
    ("complete", "vollstaendig", "Vollständig beschrieben", 0.4),
)


def _matches(needle: str, haystack: str, tokens: set[str]) -> bool:
    """Trifft ein Muster zu?

    Kurze, rein alphanumerische Muster (z. B. 'ovp', 'oc', 'riss') werden nur
    als vollstaendiges Wort akzeptiert - sonst wuerde 'oc' in 'block' oder
    'riss' in 'prisma' faelschlich anschlagen. Laengere Muster werden als
    Teilstring oder als Wort geprueft.

    Verneinungen werden beruecksichtigt: "kein Spulenfiepen" ist kein
    Spulenfiepen-Signal, "ohne Garantie" ist kein Garantiesignal.
    """
    if not needle:
        return False
    if needle.isalnum() and len(needle) <= 4 and needle not in tokens:
        return False
    return pattern_present(haystack, needle)


def analyze_text_risk(title: str | None, description: str | None = None) -> TextRiskFindings:
    """Findet Risiko- und Qualitaetssignale in unvertrauenswuerdigem Anzeigentext.

    Der Text wird ausschliesslich als Datum behandelt: Er wird normalisiert und
    gegen bekannte Muster geprueft. Anweisungen im Anzeigentext koennen das
    Bewertungssystem nicht beeinflussen.
    """
    # Titel und Beschreibung werden GETRENNT normalisiert und mit einem
    # Zeilenumbruch verbunden. Wuerde das gesamte Feld am Stueck normalisiert,
    # verschwindet der Umbruch und eine Verneinung aus der Beschreibung koennte
    # ein Signal im Titel entkräften.
    combined = f"{title or ''}\n{description or ''}"
    normalized = "\n".join(
        part for part in (normalize_text(title), normalize_text(description)) if part
    )
    tokens = set(tokenize(combined))

    findings = TextRiskFindings()
    for code, pattern, label, severity in NEGATIVE_PATTERNS:
        if _matches(normalize_text(pattern), normalized, tokens):
            findings.negatives.append(RiskSignal(code=code, label=label, severity=severity))

    for code, pattern, label, severity in POSITIVE_PATTERNS:
        if _matches(normalize_text(pattern), normalized, tokens):
            findings.positives.append(RiskSignal(code=code, label=label, severity=severity))

    return findings
