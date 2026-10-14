// Formatierung und Beschriftungen (deutsch).

const euroFormatter = new Intl.NumberFormat('de-DE', {
  style: 'currency',
  currency: 'EUR',
  maximumFractionDigits: 2,
})

const integerFormatter = new Intl.NumberFormat('de-DE')

export function euro(value: number | null | undefined): string {
  if (value === null || value === undefined) return '–'
  return euroFormatter.format(value)
}

export function number(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return '–'
  return value.toLocaleString('de-DE', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '–'
  return `${value >= 0 ? '' : ''}${value.toLocaleString('de-DE', {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  })} %`
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return '–'
  return integerFormatter.format(value)
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return 'unbekannt'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return 'unbekannt'
  return parsed.toLocaleString('de-DE', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function dateOnly(value: string | null | undefined): string {
  if (!value) return 'unbekannt'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return 'unbekannt'
  return parsed.toLocaleDateString('de-DE', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  })
}

export function ageLabel(value: string | null | undefined): string {
  if (!value) return 'Alter unbekannt'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return 'Alter unbekannt'
  const days = Math.floor((Date.now() - parsed.getTime()) / 86_400_000)
  if (days <= 0) return 'heute'
  if (days === 1) return 'vor 1 Tag'
  if (days < 31) return `vor ${days} Tagen`
  const months = Math.floor(days / 30)
  if (months < 12) return `vor ${months} Monat${months === 1 ? '' : 'en'}`
  return `vor ${Math.floor(days / 365)} Jahr${days < 730 ? '' : 'en'}`
}

export const VERDICT_LABELS: Record<string, string> = {
  outstanding: 'Herausragender Deal',
  good: 'Guter Deal',
  fair: 'Fairer Preis',
  no_deal: 'Kein besonderer Deal',
  risky: 'Potenziell riskant',
  insufficient_data: 'Zu wenig Daten',
}

export function verdictLabel(value: string | null | undefined): string {
  if (!value) return 'Nicht bewertet'
  return VERDICT_LABELS[value] ?? value
}

export const VERDICT_ORDER = [
  'outstanding',
  'good',
  'fair',
  'no_deal',
  'risky',
  'insufficient_data',
]

export const PLATFORM_LABELS: Record<string, string> = {
  ebay: 'eBay (API)',
  ebay_web: 'eBay (Website)',
  kleinanzeigen: 'Kleinanzeigen (ohne API)',
  kleinanzeigen_web: 'Kleinanzeigen (Website)',
  local_import: 'Lokaler Import',
}

export function platformLabel(value: string): string {
  return PLATFORM_LABELS[value] ?? value
}

export const CONDITION_LABELS: Record<number, string> = {
  0: 'Defekt / Ersatzteil',
  1: 'Akzeptabel',
  2: 'Gebraucht, gut',
  3: 'Sehr gut / wie neu',
  4: 'Neu / ungeöffnet',
}

export function conditionLabel(rank: number | null | undefined): string {
  if (rank === null || rank === undefined) return 'Zustand unbekannt'
  return CONDITION_LABELS[rank] ?? 'Zustand unbekannt'
}

export const RUN_STATUS_LABELS: Record<string, string> = {
  pending: 'In der Warteschlange',
  running: 'Läuft',
  paused: 'Pausiert',
  completed: 'Abgeschlossen',
  cancelled: 'Abgebrochen',
  failed: 'Fehlgeschlagen',
}

export function runStatusLabel(value: string): string {
  return RUN_STATUS_LABELS[value] ?? value
}

export const SORT_LABELS: Record<string, string> = {
  best_deal: 'Bester Deal',
  price_advantage: 'Höchster Preisvorteil',
  lowest_price: 'Niedrigster Gesamtpreis',
  price_performance: 'Beste Preis-Leistung',
  newest: 'Neueste Angebote',
  confidence: 'Höchste Bewertungssicherheit',
}

export const FAMILY_LABELS: Record<string, string> = {
  exact_model: 'Vollständige Produktbezeichnung',
  manufacturer_model: 'Hersteller und Modell',
  abbreviation: 'Abkürzung',
  alternate_spelling: 'Alternative Schreibweise',
  typo: 'Häufiger Tippfehler',
  community_name: 'Community-Bezeichnung',
  model_number: 'Modellnummer',
  variant: 'Modellvariante',
  title_keyword: 'Titel-Schlüsselwort',
  description_hint: 'Beschreibungs-Hinweis',
  incomplete_title: 'Unvollständiger Titel',
  platform_specific: 'Plattformspezifisch',
  generic: 'Kategorie-Sammelbegriff',
}

export function familyLabel(value: string | null | undefined): string {
  if (!value) return '–'
  return FAMILY_LABELS[value] ?? value
}

export function daysBackOptions(): { value: number; label: string }[] {
  return [
    { value: 1, label: '24 Stunden' },
    { value: 7, label: '7 Tage' },
    { value: 30, label: '30 Tage' },
    { value: 90, label: '90 Tage' },
    { value: 150, label: '150 Tage (Standard)' },
    { value: 365, label: '1 Jahr' },
  ]
}

export function scoreTone(score: number | null | undefined): string {
  if (score === null || score === undefined) return 'neutral'
  if (score >= 80) return 'great'
  if (score >= 70) return 'good'
  if (score >= 55) return 'ok'
  return 'bad'
}
