// Typisierter Client für die lokale Backend-API.
//
// Bewusst schmal: nur was die Oberfläche wirklich braucht (Kategorien,
// Plattformen, Einstellungen, Suchlauf, Angebotsliste). Fehler werden mit der
// Meldung des Backends weitergegeben, damit echte Ursachen sichtbar sind.

import type {
  ConnectorCapabilities,
  ListingList,
  PriceLimitAdvice,
  PriceList,
  RevalidationResult,
  Run,
  RunSummary,
  RuntimeSettings,
  SearchRequest,
  SettingsPayload,
} from './types'

const BASE = '/api'

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function messageFromDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === 'object') {
          const record = item as Record<string, unknown>
          const location = Array.isArray(record.loc) ? record.loc.join('.') : ''
          const msg = typeof record.msg === 'string' ? record.msg : 'Ungültiger Wert'
          return location ? `${location}: ${msg}` : msg
        }
        return String(item)
      })
      .join(' | ')
  }
  if (detail && typeof detail === 'object') {
    const record = detail as Record<string, unknown>
    if (typeof record.message === 'string') return record.message
    return JSON.stringify(detail)
  }
  return ''
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    throw new ApiError(
      'Das Backend ist nicht erreichbar. Läuft der Server unter http://127.0.0.1:8000? ' +
        'Starte ihn mit "python -m backend.main" oder über das Startskript.',
      0,
    )
  }
  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === 'object'
        ? messageFromDetail((payload as Record<string, unknown>).detail)
        : ''
    throw new ApiError(
      detail || `Anfrage fehlgeschlagen (HTTP ${response.status})`,
      response.status,
    )
  }
  return payload as T
}

function query(params: Record<string, unknown>): string {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === '') return
    if (Array.isArray(value)) {
      value.forEach((entry) => search.append(key, String(entry)))
      return
    }
    search.append(key, String(value))
  })
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  connectors: () => request<ConnectorCapabilities[]>('/connectors'),

  settings: () => request<SettingsPayload>('/settings'),

  // Komponenten und Kauf-Limits aus "Höchstpreise.txt" (live eingelesen).
  priceLimits: () => request<PriceList>('/price-limits'),

  // Vorschläge für die Kauf-Limits aus den beobachteten Angebotspreisen.
  priceLimitAdvice: () => request<PriceLimitAdvice>('/price-limits/suggestions'),

  // Übernimmt die Vorschläge in "Höchstpreise.txt" (legt vorher eine Sicherung an).
  applyPriceLimitAdvice: () =>
    request<PriceLimitAdvice>('/price-limits/apply', { method: 'POST' }),

  saveSettings: (runtime: RuntimeSettings) =>
    request<SettingsPayload>('/settings', {
      method: 'PUT',
      body: JSON.stringify(runtime),
    }),

  // Leert Angebote und Suchverlauf komplett (Einstellungen bleiben erhalten).
  clearData: () =>
    request<{ cleared: Record<string, number>; cancelled_runs: number[]; message: string }>(
      '/maintenance/clear',
      { method: 'POST' },
    ),

  startRun: (payload: SearchRequest) =>
    request<{ run_id: number; status: string; message: string }>('/search/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  run: (id: number) => request<Run>(`/search/runs/${id}`),

  // Suchlaeufe, neueste zuerst. Wird beim Laden gebraucht, um einen noch
  // laufenden Suchlauf wieder anzuzeigen (der Zustand liegt im Backend).
  runs: (limit = 5) => request<RunSummary[]>(`/search/runs${query({ limit })}`),

  cancelRun: (id: number) =>
    request<{ message: string }>(`/search/runs/${id}/cancel`, { method: 'POST' }),

  listings: (params: Record<string, unknown>) =>
    request<ListingList>(`/listings${query(params)}`),

  // Prüft den gespeicherten Bestand erneut (Titelprüfung) und räumt ihn auf.
  revalidateListings: () =>
    request<RevalidationResult>('/listings/revalidate', { method: 'POST' }),
}
