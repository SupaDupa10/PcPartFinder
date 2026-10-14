import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import type {
  ConnectorCapabilities,
  DeliveryMode,
  Listing,
  ListingList,
  PriceList,
  Run,
  RuntimeSettings,
  SearchRequest,
} from './types'
import { count, euro, platformLabel, runStatusLabel, verdictLabel } from './format'

// Diese Oberfläche hat genau eine Aufgabe: gute Angebote finden und den Link
// dazu zeigen. Kein Dashboard, keine Auswertungen, keine Zwischenberichte.

const PAGE_SIZE = 40
// Obergrenze der Angebotsliste im Backend (siehe backend/api/listings.py).
const PAGE_SIZE_MAX = 500

// Schwellen für "gutes Angebot" (Deal-Score des Backends, 0-100).
const QUALITY_LEVELS: { value: number; label: string }[] = [
  { value: 0, label: 'Alle Angebote' },
  { value: 55, label: 'Brauchbar (ab 55)' },
  { value: 65, label: 'Gut (ab 65)' },
  { value: 75, label: 'Sehr gut (ab 75)' },
]

const SORTS: { value: string; label: string }[] = [
  { value: 'best_deal', label: 'Beste zuerst' },
  { value: 'lowest_price', label: 'Günstigste zuerst' },
  { value: 'newest', label: 'Neueste zuerst' },
]

const ACTIVE_STATUSES = new Set(['running', 'pending', 'paused'])

// Der Radius gilt nur, wenn der Verkäuferstandort eine Rolle spielt.
const RADIUS_OPTIONS: { value: number | null; label: string }[] = [
  { value: 10, label: '10 km' },
  { value: 25, label: '25 km' },
  { value: 50, label: '50 km' },
  { value: 100, label: '100 km' },
  { value: 200, label: '200 km' },
  { value: null, label: 'Ohne Umkreis' },
]

const DELIVERY_OPTIONS: { value: DeliveryMode; label: string }[] = [
  { value: 'both', label: 'Versand und Abholung' },
  { value: 'shipping', label: 'Nur Versand' },
  { value: 'pickup', label: 'Nur Abholung' },
]

interface Selection {
  // Gruppen der Kauf-Limit-Liste (Höchstpreise.txt). Eine Suchleiste gibt es
  // nicht mehr: Gesucht wird immer nach den Komponenten der Liste, jede nur
  // bis zu ihrem dort hinterlegten Preis.
  groups: string[]
  platforms: string[]
  minScore: number
  sort: string
  delivery: DeliveryMode
  postalCode: string
  radiusKm: number | null
}

const DEFAULT_SELECTION: Selection = {
  groups: [],
  platforms: [],
  minScore: 65,
  sort: 'best_deal',
  delivery: 'both',
  postalCode: '',
  radiusKm: 50,
}

// Sonderwert der Gruppenauswahl: alle Gruppen der Liste auf einmal.
const ALL_GROUPS = '__all__'

export default function App() {
  const [connectors, setConnectors] = useState<ConnectorCapabilities[]>([])
  const [runtime, setRuntime] = useState<RuntimeSettings | null>(null)
  const [priceList, setPriceList] = useState<PriceList | null>(null)
  const [selection, setSelection] = useState<Selection>(DEFAULT_SELECTION)

  const [listings, setListings] = useState<ListingList | null>(null)
  const [loadingListings, setLoadingListings] = useState(false)
  const [run, setRun] = useState<Run | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'error' } | null>(null)
  const [ready, setReady] = useState(false)
  const [checkingStock, setCheckingStock] = useState(false)
  const [updatingLimits, setUpdatingLimits] = useState(false)
  const [offset, setOffset] = useState(0)
  const [showSettings, setShowSettings] = useState(false)
  // Nach einer Suche zeigt die Liste nur die Angebote dieses Laufs.
  const [runScope, setRunScope] = useState<number | null>(null)
  const pollRef = useRef<number | null>(null)
  // Wie viele Angebote die Liste gerade zeigt. Die Live-Anzeige holt genauso
  // viele nach, damit eine aufgeklappte Liste nicht wieder zuklappt.
  const listWindow = useRef(PAGE_SIZE)
  // Laufende Nummer der Listenabrufe: Eine ältere, langsamer eintreffende
  // Antwort darf eine neuere nicht überschreiben.
  const requestSeq = useRef(0)
  // Zwischenstand des Laufs, zu dem die Liste zuletzt nachgeladen wurde.
  const liveStand = useRef<string | null>(null)

  const usablePlatforms = useMemo(
    () =>
      connectors
        .filter((entry) => entry.available && entry.supports.search === true)
        .map((entry) => entry.platform),
    [connectors],
  )

  const priceGroups = useMemo(() => priceList?.groups ?? [], [priceList])
  const chosenGroups = useMemo(
    () => priceGroups.filter((entry) => selection.groups.includes(entry.key)),
    [priceGroups, selection.groups],
  )
  const groupValue = chosenGroups.length > 1 ? ALL_GROUPS : (selection.groups[0] ?? '')

  const tell = useCallback((text: string, tone: 'ok' | 'error' = 'ok') => {
    setNotice({ text, tone })
    window.setTimeout(() => setNotice(null), 6000)
  }, [])

  // -- Laden: Kategorien, Plattformen, Einstellungen -----------------------

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [caps, payload, limits] = await Promise.all([
          api.connectors(),
          api.settings(),
          api.priceLimits(),
        ])
        if (cancelled) return
        setConnectors(caps)
        setRuntime(payload.runtime)
        setPriceList(limits)
        setSelection((previous) => ({
          ...previous,
          // Ohne eigene Auswahl wird die erste Gruppe der Liste durchsucht.
          groups:
            previous.groups.length > 0
              ? previous.groups
              : limits.groups.length > 0
                ? [limits.groups[0].key]
                : [],
          minScore: payload.runtime.default_min_deal_score ?? previous.minScore,
        }))
        // Der letzte Suchlauf wird auch nach einem Neuladen uebernommen: Sein
        // Zustand liegt im Backend, nicht in dieser Seite. Ohne das waere der
        // Fortschritt nach jedem Neuladen verschwunden, obwohl die Suche laeuft.
        try {
          const [newest] = await api.runs(1)
          if (!cancelled && newest !== undefined) {
            const full = await api.run(newest.id)
            if (!cancelled) {
              setRun(full)
              if (ACTIVE_STATUSES.has(full.status)) setRunScope(full.id)
            }
          }
        } catch {
          // Ohne Verlauf startet die Oberflaeche einfach ohne laufende Suche.
        }
      } catch (exc) {
        if (!cancelled) {
          tell(
            `Backend nicht erreichbar: ${exc instanceof Error ? exc.message : String(exc)}`,
            'error',
          )
        }
      } finally {
        if (!cancelled) setReady(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [tell])

  // Läuft gerade ein Suchlauf? Das entscheidet, ob die Liste den Zwischenstand
  // zeigt (unbewertet, ohne Qualitätsgrenze) oder das bewertete Ergebnis.
  const activeRunId = run !== null && ACTIVE_STATUSES.has(run.status) ? run.id : null

  // -- Angebote laden ------------------------------------------------------

  const loadListings = useCallback(
    // ``scopeOverride`` erlaubt es, die Liste bewusst ohne Lauf-Filter zu holen:
    // Ein abgebrochener Lauf ohne eigene Treffer wuerde sonst eine leere Liste
    // zeigen, obwohl gespeicherte Angebote vorhanden sind.
    //
    // ``live`` holt den Zwischenstand eines laufenden Suchlaufs: Dann zaehlt die
    // Qualitaetsgrenze nicht - die Bewertung laeuft erst am Ende des Laufs,
    // vorher traegt kein Angebot eine Punktzahl. Mit Grenze waere die Liste bis
    // zum Schluss leer.
    async (
      nextOffset: number,
      extend: boolean,
      scopeOverride?: number | null,
      live = false,
    ) => {
      setLoadingListings(true)
      const scope = scopeOverride === undefined ? runScope : scopeOverride
      const seq = requestSeq.current + 1
      requestSeq.current = seq
      // "Mehr anzeigen" erweitert das Fenster dauerhaft; das Nachladen der
      // Live-Anzeige laesst es unveraendert.
      if (!live) listWindow.current = extend ? nextOffset + PAGE_SIZE : PAGE_SIZE
      try {
        const result = await api.listings({
          sort: selection.sort,
          limit: live
            ? Math.min(PAGE_SIZE_MAX, Math.max(PAGE_SIZE, listWindow.current))
            : PAGE_SIZE,
          offset: nextOffset,
          min_score: live ? undefined : selection.minScore || undefined,
          fixed_price_only: true,
          // "Versand und Abholung" ist keine Einschränkung - dann bleibt der
          // Parameter weg. "Nur Versand" blendet Angebote aus, die belegt nur
          // abgeholt werden können, "Nur Abholung" solche ohne Standort.
          delivery: selection.delivery === 'both' ? undefined : selection.delivery,
          new_since_run: scope ?? undefined,
        })
        if (seq !== requestSeq.current) return
        setListings((previous) =>
          extend && previous
            ? { ...result, items: [...previous.items, ...result.items] }
            : result,
        )
        setOffset(nextOffset)
      } catch (exc) {
        if (seq === requestSeq.current) {
          tell(exc instanceof Error ? exc.message : String(exc), 'error')
        }
      } finally {
        if (seq === requestSeq.current) setLoadingListings(false)
      }
    },
    [selection.minScore, selection.sort, selection.delivery, runScope, tell],
  )

  useEffect(() => {
    if (!ready) return
    // Läuft gerade eine Suche, wird der Zwischenstand geladen: Mit der
    // Qualitätsgrenze wäre die Liste beim Umschalten der Sortierung leer, weil
    // während des Laufs noch nichts bewertet ist.
    void loadListings(0, false, undefined, activeRunId !== null)
  }, [ready, loadListings])

  // -- Bestand prüfen ------------------------------------------------------

  // Der gespeicherte Bestand wächst über viele Suchläufe. Die Titelprüfung wird
  // dabei immer genauer - hier lässt sich das sofort auf alles Gespeicherte
  // anwenden, ohne auf den nächsten Suchlauf zu warten.
  const revalidateStock = useCallback(async () => {
    setCheckingStock(true)
    try {
      const result = await api.revalidateListings()
      tell(result.summary)
      await loadListings(0, false)
    } catch (exc) {
      tell(exc instanceof Error ? exc.message : String(exc), 'error')
    } finally {
      setCheckingStock(false)
    }
  }, [loadListings, tell])

  // -- Kauf-Limits nachführen ----------------------------------------------

  // Die handgepflegte Liste veraltet (die letzte Anpassung war eine pauschale
  // Anhebung von Hand). Hier wird je Komponente ein Limit aus den tatsächlich
  // beobachteten Angeboten abgeleitet und in die Datei geschrieben - mit
  // Sicherungskopie. Ohne ausreichende Datenbasis bleibt ein Limit unverändert.
  const updatePriceLimits = useCallback(async () => {
    setUpdatingLimits(true)
    try {
      const result = await api.applyPriceLimitAdvice()
      tell(result.summary || 'Kauf-Limits wurden geprüft.')
      setPriceList(await api.priceLimits())
    } catch (exc) {
      tell(exc instanceof Error ? exc.message : String(exc), 'error')
    } finally {
      setUpdatingLimits(false)
    }
  }, [tell])

  // -- Laufender Suchlauf --------------------------------------------------

  // Der Fortschritt wird zyklisch beim Backend abgefragt, solange der Lauf
  // laeuft. Der Zeitgeber haengt bewusst nur an der Lauf-Nummer: Sonst wuerde
  // ihn jede neue Fortschrittsmeldung neu aufsetzen und die Anzeige haekeln.
  useEffect(() => {
    if (activeRunId === null) return
    const tick = async () => {
      try {
        const current = await api.run(activeRunId)
        setRun(current)
        if (!ACTIVE_STATUSES.has(current.status)) {
          if (pollRef.current !== null) {
            window.clearInterval(pollRef.current)
            pollRef.current = null
          }
          // Hat der Lauf nichts gespeichert (z. B. frueh abgebrochen), waere die
          // auf ihn eingegrenzte Liste leer - gezeigt werden dann die
          // vorhandenen Angebote.
          const leer =
            current.result_count === 0 && current.new_count + current.updated_count === 0
          if (leer) setRunScope(null)
          await loadListings(0, false, leer ? null : undefined)
          tell(`Suche beendet: ${count(current.result_count)} Angebote gefunden.`)
        }
      } catch {
        // Ein einzelner fehlgeschlagener Statusabruf darf die Suche nicht stören.
      }
    }
    void tick()
    pollRef.current = window.setInterval(tick, 1500)
    return () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [activeRunId, loadListings, tell])

  // -- Zwischenstand waehrend des Laufs ------------------------------------

  // Das Backend speichert jedes brauchbare Angebot sofort, naemlich je
  // Suchanfrage (siehe ``backend/search/runner.py``). Die Liste wurde aber
  // frueher erst nach dem Ende des Laufs neu geladen und blieb damit bis zum
  // Schluss leer, obwohl der Bestand schon wuchs.
  //
  // Deshalb wird die Liste waehrend eines Laufs nachgeladen, sobald der Lauf
  // mehr gespeicherte oder bewertete Angebote meldet. Die Qualitaetsgrenze gilt
  // dabei nicht (siehe ``loadListings``) - die Bewertung laeuft erst am Ende.
  // Am Ende des Laufs laedt der Zeitgeber oben die Liste erneut, dann mit
  // Grenze.
  const laufStand =
    `${run?.progress?.new_listings ?? run?.new_count ?? 0}` +
    `|${run?.progress?.updated_listings ?? run?.updated_count ?? 0}` +
    `|${run?.progress?.scored ?? 0}`

  useEffect(() => {
    if (activeRunId === null) {
      liveStand.current = null
      return
    }
    if (liveStand.current === laufStand) return
    liveStand.current = laufStand
    void loadListings(0, false, activeRunId, true)
  }, [activeRunId, laufStand, loadListings])

  const startSearch = async () => {
    if (selection.groups.length === 0) {
      tell(
        'Keine Komponenten gewählt. In der Kauf-Limit-Liste (Höchstpreise.txt) steht keine passende Gruppe.',
        'error',
      )
      return
    }
    const postalCode = selection.postalCode.trim()
    if (selection.delivery === 'pickup' && !postalCode) {
      tell('Für die Suche nach Abholung wird die Postleitzahl des Standorts benötigt.', 'error')
      return
    }
    const platforms = selection.platforms.length ? selection.platforms : usablePlatforms
    if (platforms.length === 0) {
      tell('Keine Plattform verfügbar. Läuft das Backend und ist eine Quelle aktiv?', 'error')
      return
    }
    setBusy(true)
    try {
      const payload: SearchRequest = {
        product: null,
        categories: [],
        price_groups: selection.groups,
        platforms,
        max_price: null,
        price_per_category: {},
        delivery: selection.delivery,
        postal_code: postalCode || null,
        radius_km: selection.delivery === 'shipping' ? null : selection.radiusKm,
        days_back: runtime?.default_days_back ?? 150,
        min_condition_rank: null,
        only_working: false,
        seller_location: null,
        min_deal_score: selection.minScore,
        min_price_advantage_pct: null,
        only_new_since_last_run: false,
        exclude_problematic: true,
        preferred_models: [],
        max_rounds: null,
      }
      const result = await api.startRun(payload)
      setRunScope(result.run_id)
      setRun({
        id: result.run_id,
        status: 'running',
        created_at: new Date().toISOString(),
        started_at: null,
        finished_at: null,
        request: payload,
        plan: null,
        plan_source: null,
        progress: null,
        coverage: null,
        errors: null,
        limitations: null,
        queries_total: 0,
        queries_done: 0,
        result_count: 0,
        new_count: 0,
        updated_count: 0,
      })
    } catch (exc) {
      tell(exc instanceof Error ? exc.message : String(exc), 'error')
    } finally {
      setBusy(false)
    }
  }

  const stopSearch = async () => {
    if (run === null) return
    try {
      await api.cancelRun(run.id)
      const current = await api.run(run.id)
      setRun(current)
    } catch (exc) {
      tell(exc instanceof Error ? exc.message : String(exc), 'error')
    }
  }

  // Alles Gespeicherte auf null: Angebote und Suchverlauf. Die gespeicherten
  // Angebote stammen aus frueheren Suchen und wuerden sonst fuer immer in der
  // Ergebnisliste stehen bleiben, auch wenn die Titelpruefung inzwischen
  // besser aussortiert.
  const clearStoredData = async () => {
    const confirmed = window.confirm(
      'Wirklich alle gespeicherten Angebote und den kompletten Suchverlauf löschen?\n\n' +
        'Einstellungen und Wissensbasis bleiben erhalten. Das lässt sich nicht rückgängig machen.',
    )
    if (!confirmed) return
    setBusy(true)
    try {
      if (active) await stopSearch()
      const result = await api.clearData()
      setRun(null)
      // runScope loest das Nachladen aus, wenn vorher nach einem Lauf gefiltert
      // wurde; der ausdrueckliche Aufruf deckt den Fall ohne Lauf ab.
      setRunScope(null)
      await loadListings(0, false)
      tell(result.message)
    } catch (exc) {
      tell(exc instanceof Error ? exc.message : String(exc), 'error')
    } finally {
      setBusy(false)
    }
  }

  const saveRuntime = async (changes: Partial<RuntimeSettings>) => {
    if (runtime === null) return
    const next = { ...runtime, ...changes }
    setRuntime(next)
    try {
      const payload = await api.saveSettings(next)
      setRuntime(payload.runtime)
    } catch (exc) {
      tell(exc instanceof Error ? exc.message : String(exc), 'error')
    }
  }

  const active = run !== null && ACTIVE_STATUSES.has(run.status)
  const items = listings?.items ?? []

  return (
    <div className="page">
      <header className="head">
        <h1>Angebote finden</h1>
        <span className="sub">
          Durchsucht eBay (Website) und Kleinanzeigen (Website) nach den Komponenten aus
          der Kauf-Limit-Liste und zeigt nur Angebote unter dem jeweiligen Limit.
        </span>
      </header>

      <section className="panel">
        <div className="controls">
          <div className="field grow">
            <label htmlFor="group">Welche Komponenten? (aus Höchstpreise.txt)</label>
            <select
              id="group"
              value={groupValue}
              disabled={priceGroups.length === 0}
              onChange={(event) => {
                const value = event.target.value
                setSelection({
                  ...selection,
                  groups:
                    value === ALL_GROUPS
                      ? priceGroups.map((entry) => entry.key)
                      : value
                        ? [value]
                        : [],
                })
              }}
            >
              {priceGroups.map((entry) => (
                <option key={entry.key} value={entry.key}>
                  {entry.label} – {entry.count} Komponenten, bis {euro(entry.max_price)}
                </option>
              ))}
              {priceGroups.length > 1 && (
                <option value={ALL_GROUPS}>Alle Gruppen der Liste</option>
              )}
            </select>
          </div>

          <div className="field small">
            <label htmlFor="postal">Standort (PLZ)</label>
            <input
              id="postal"
              type="text"
              inputMode="numeric"
              value={selection.postalCode}
              placeholder="z. B. 10115"
              onChange={(event) =>
                setSelection({ ...selection, postalCode: event.target.value })
              }
            />
          </div>

          <div className="field small">
            <label htmlFor="radius">Umkreis</label>
            <select
              id="radius"
              value={selection.radiusKm === null ? 'none' : String(selection.radiusKm)}
              disabled={selection.delivery === 'shipping'}
              onChange={(event) =>
                setSelection({
                  ...selection,
                  radiusKm: event.target.value === 'none' ? null : Number(event.target.value),
                })
              }
            >
              {RADIUS_OPTIONS.map((option) => (
                <option key={option.label} value={option.value === null ? 'none' : option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field small">
            <label htmlFor="delivery">Lieferung</label>
            <select
              id="delivery"
              value={selection.delivery}
              onChange={(event) =>
                setSelection({ ...selection, delivery: event.target.value as DeliveryMode })
              }
            >
              {DELIVERY_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field small">
            <label htmlFor="quality">Mindestqualität</label>
            <select
              id="quality"
              value={selection.minScore}
              onChange={(event) =>
                setSelection({ ...selection, minScore: Number(event.target.value) })
              }
            >
              {QUALITY_LEVELS.map((level) => (
                <option key={level.value} value={level.value}>
                  {level.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field small">
            <label htmlFor="source">Quelle</label>
            <select
              id="source"
              value={selection.platforms[0] ?? ''}
              onChange={(event) =>
                setSelection({
                  ...selection,
                  platforms: event.target.value ? [event.target.value] : [],
                })
              }
            >
              <option value="">Alle verfügbaren</option>
              {connectors.map((entry) => (
                <option key={entry.platform} value={entry.platform} disabled={!entry.available}>
                  {entry.label}
                  {entry.available ? '' : ' (nicht verfügbar)'}
                </option>
              ))}
            </select>
          </div>

          <button
            className="primary"
            onClick={startSearch}
            disabled={busy || active || selection.groups.length === 0}
          >
            {active ? 'Suche läuft …' : 'Alle Komponenten suchen'}
          </button>

          {active && (
            <button className="ghost" onClick={stopSearch}>
              Abbrechen
            </button>
          )}

          <p className="field hint">
            Gesucht wird nach jeder Komponente dieser Gruppe - auf beiden Quellen, und
            jeweils nur bis zu ihrem Kauf-Limit inkl. Versand. Angebote darüber
            erscheinen gar nicht; unter einem Viertel des Limits (dem Mindestpreis)
            wird ebenfalls nichts gespeichert - das ist fast immer Zubehör, ein
            Einzelteil oder defekte Ware. Die Liste wächst mit: Wird Höchstpreise.txt
            erweitert, sind die neuen Komponenten beim nächsten Suchlauf dabei.
            Die Lieferungsauswahl gilt auch für die Liste: Bei „Nur Versand“ verschwinden
            Angebote, die nur abgeholt werden können.
          </p>
        </div>

        {run && <RunProgress run={run} active={active} />}
        {!active && usablePlatforms.length === 0 && (
          <p className="status warn">
            Keine Quelle ist gerade nutzbar. Prüfe im Backend die Connector-Einstellungen.
          </p>
        )}
        {priceGroups.length === 0 && priceList && (
          <p className="status warn">
            Die Kauf-Limit-Liste ist leer oder nicht lesbar ({priceList.path || 'kein Pfad gesetzt'}).
            {priceList.errors.length > 0 ? ` ${priceList.errors[0]}` : ''}
          </p>
        )}
        {!active && chosenGroups.length > 0 && (
          <p className="status">
            {chosenGroups.map((entry) => `${entry.label} (${entry.count})`).join(' + ')} –
            Kauf-Limits von {euro(Math.min(...chosenGroups.map((entry) => entry.min_price)))} bis{' '}
            {euro(Math.max(...chosenGroups.map((entry) => entry.max_price)))}. Je Komponente gilt
            zusätzlich ein Mindestpreis von einem Viertel ihres Limits.
          </p>
        )}
      </section>

      <section className="panel">
        <div className="result-head">
          <h2>{count(listings?.total ?? 0)} Angebote</h2>
          <div className="controls tight">
            {loadingListings && <span className="spinner" />}
            <button
              className="ghost"
              disabled={checkingStock}
              title="Prüft alle gespeicherten Angebote erneut und nimmt Kartons, Teile, Netzteile und Komplett-Rechner aus der Liste."
              onClick={() => void revalidateStock()}
            >
              {checkingStock ? 'Bestand wird geprüft…' : 'Bestand prüfen'}
            </button>
            <select
              value={selection.sort}
              onChange={(event) => setSelection({ ...selection, sort: event.target.value })}
              aria-label="Sortierung"
            >
              {SORTS.map((entry) => (
                <option key={entry.value} value={entry.value}>
                  {entry.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {active && (
          <p className="status">
            Suche läuft: Die Liste zeigt jedes Angebot dieses Laufs, sobald es gefunden
            und gespeichert ist - nicht erst am Ende. Die Bewertung läuft zum Schluss,
            deshalb stehen die neuen Zeilen bis dahin ohne Punktzahl hier, und die
            Mindestqualität ist noch nicht angewendet.
          </p>
        )}

        {items.length === 0 && !loadingListings && (
          <p className="status">
            {active
              ? 'In diesem Lauf wurde noch nichts gespeichert. Die Liste füllt sich, sobald die ersten Angebote gefunden sind.'
              : 'Noch keine Angebote. Starte oben eine Suche.'}
          </p>
        )}

        <ul className="offers">
          {items.map((listing) => (
            <Offer key={listing.id} listing={listing} />
          ))}
        </ul>

        {listings && items.length < listings.total && (
          <button
            className="ghost wide"
            disabled={loadingListings}
            onClick={() => void loadListings(offset + PAGE_SIZE, true)}
          >
            Mehr anzeigen ({count(Math.max(0, listings.total - items.length))} weitere)
          </button>
        )}
      </section>

      <section className="panel">
        <button className="linklike" onClick={() => setShowSettings((value) => !value)}>
          {showSettings ? 'Einstellungen ausblenden' : 'Einstellungen'}
        </button>

        {showSettings && runtime && (
          <div className="settings">
            <div className="controls">
              <div className="field small">
                <label htmlFor="search-pages">Seiten je Suchbegriff</label>
                <input
                  id="search-pages"
                  type="number"
                  min={1}
                  max={40}
                  value={runtime.search_max_pages_per_query}
                  onChange={(event) =>
                    setRuntime({
                      ...runtime,
                      search_max_pages_per_query: Number(event.target.value),
                    })
                  }
                  onBlur={() =>
                    void saveRuntime({
                      search_max_pages_per_query: runtime.search_max_pages_per_query,
                    })
                  }
                />
              </div>
              <div className="field small">
                <label htmlFor="search-parallel-pages">Seiten gleichzeitig</label>
                <input
                  id="search-parallel-pages"
                  type="number"
                  min={1}
                  max={8}
                  value={runtime.search_parallel_pages_per_query}
                  onChange={(event) =>
                    setRuntime({
                      ...runtime,
                      search_parallel_pages_per_query: Number(event.target.value),
                    })
                  }
                  onBlur={() =>
                    void saveRuntime({
                      search_parallel_pages_per_query: runtime.search_parallel_pages_per_query,
                    })
                  }
                />
              </div>
            </div>

            <div className="controls">
              <button className="ghost" onClick={() => void clearStoredData()} disabled={busy}>
                Alle Angebote und den Suchverlauf löschen
              </button>
              <button
                className="ghost"
                onClick={() => void updatePriceLimits()}
                disabled={updatingLimits || busy}
              >
                {updatingLimits ? 'Kauf-Limits werden nachgeführt …' : 'Kauf-Limits nachführen'}
              </button>
            </div>

            <p className="field hint">
              „Kauf-Limits nachführen“ leitet je Komponente ein Limit aus dem
              günstigsten Zehntel der gespeicherten Angebote ab und schreibt es in
              „Höchstpreise.txt“ (vorher entsteht eine Sicherungskopie). Ohne
              ausreichende Datenbasis bleibt ein Limit unverändert.
            </p>

            <p className="field hint">
              „Alle Angebote und den Suchverlauf löschen“ setzt die Ergebnisliste auf
              null - gedacht als Neuanfang, wenn in früheren Suchen noch Angebote
              gespeichert wurden, die nicht zum gesuchten Produkt gehören (Verpackung,
              Einzelteile, fremde Modelle). Einstellungen und Wissensbasis bleiben
              unverändert; der nächste Suchlauf füllt die Liste neu.
            </p>
          </div>
        )}
      </section>

      {notice && (
        <div className={notice.tone === 'error' ? 'notice error' : 'notice'}>{notice.text}</div>
      )}
    </div>
  )
}

// Fortschritt des Suchlaufs: live mitzuverfolgen, weil alle Zahlen aus dem
// Backend kommen (alle 1,5 s neu geholt) und nur Laufzeit und "aktualisiert
// vor …" lokal weiterticken. Der Lauf selbst wird beim Laden aus dem Backend
// uebernommen, damit die Anzeige auch nach einem Neuladen stimmt.
function RunProgress({ run, active }: { run: Run; active: boolean }) {
  const progress = run.progress
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])

  const done = progress?.queries_done ?? run.queries_done
  const total = progress?.queries_total ?? run.queries_total
  // Der Balken laeuft mit Bruchteilen von Prozent, die Prozentzahl daneben
  // gerundet: Bei 198 Anfragen waere ein Balken in ganzen Prozenten sekundenlang
  // unveraendert und saehe aus, als haenge die Suche.
  const share = total > 0 ? Math.min(100, (done / total) * 100) : 0
  const shareLabel = total > 0 ? Math.round(share) : 0
  const laufzeit = secondsSince(progress?.started_at ?? run.started_at, active ? now : null)
  const stand = active ? ageSeconds(progress?.updated_at, now) : null
  const platforms = progress?.platforms ?? []
  const errors = progress?.errors ?? run.errors ?? []
  const limitations = progress?.limitations ?? []

  if (!active) {
    // "gefunden" zaehlt die bewerteten Angebote des Laufs. Wurde der Lauf
    // vorzeitig beendet, ist die Zahl 0 - dann ist die Zahl der gespeicherten
    // Angebote die ehrlichere Aussage.
    const gespeichert = progress?.new_listings ?? run.new_count
    const teile = [
      run.result_count > 0
        ? `${count(run.result_count)} Angebote gefunden`
        : `${count(gespeichert)} Angebote gespeichert`,
      run.result_count > 0 && run.new_count > 0 ? `${count(run.new_count)} neu` : null,
      titleCheckLine(run),
      selectionLine(run),
    ].filter((entry): entry is string => Boolean(entry))
    return (
      <p className="status">
        Letzter Lauf ({runStatusLabel(run.status)}): {teile.join(' · ')}
      </p>
    )
  }

  const zahlen: { label: string; value: string }[] = [
    { label: 'Rohtreffer', value: count(progress?.raw_hits) },
    { label: 'davon brauchbar', value: count(progress?.raw_hits_usable) },
    { label: 'neu gespeichert', value: count(progress?.new_listings) },
    { label: 'aktualisiert', value: count(progress?.updated_listings) },
  ]
  if (progress?.rounds !== undefined) {
    zahlen.push({ label: 'Runde', value: count(progress.rounds) })
  }
  if (laufzeit !== null) {
    zahlen.push({ label: 'Laufzeit', value: durationLabel(laufzeit) })
  }

  return (
    <div className="progress">
      <div className="progress-head">
        <strong>{progress?.phase_label ?? 'Suche läuft'}</strong>
        <span className="badge">{runStatusLabel(run.status)}</span>
      </div>
      <div
        className="progress-bar"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={total > 0 ? total : 0}
        aria-valuenow={total > 0 ? done : undefined}
        aria-label="Anfragen"
      >
        <i style={{ width: `${share}%` }} />
      </div>
      <p className="status">
        {count(done)} / {count(total)} Anfragen ({shareLabel} %)
        {stand !== null && ` · aktualisiert vor ${stand} s`}
        {progress?.paused ? ' · pausiert' : ''}
      </p>
      <ul className="progress-numbers">
        {zahlen.map((entry) => (
          <li key={entry.label}>
            <span>{entry.label}</span>
            <strong>{entry.value}</strong>
          </li>
        ))}
      </ul>
      {platforms.length > 0 && (
        <ul className="progress-platforms">
          {platforms.map((entry) => (
            <li key={entry.platform}>
              <span className="progress-platform-name">
                {entry.label}
                {entry.status ? ` · ${entry.status}` : ''}
              </span>
              <span className="progress-platform-numbers">
                {count(entry.queries_done)} / {count(entry.queries_total)} Anfragen ·{' '}
                {count(entry.results_returned)} Treffer · {count(entry.results_usable)} brauchbar
                {entry.errors > 0 ? ` · ${count(entry.errors)} Fehler` : ''}
              </span>
            </li>
          ))}
        </ul>
      )}
      {progress?.message && <p className="status">{progress.message}</p>}
      {titleCheckLine(run) && <p className="status">{titleCheckLine(run)}</p>}
      {selectionLine(run) && <p className="status">{selectionLine(run)}</p>}
      {limitations.slice(0, 2).map((entry) => (
        <p className="status warn" key={entry}>
          {entry}
        </p>
      ))}
      {errors.length > 0 && <p className="status warn">Fehler: {errors[errors.length - 1]}</p>}
    </div>
  )
}

// Sekunden seit einem Zeitstempel. Ohne ``bis`` gilt der aktuelle Zeitpunkt;
// im laufenden Betrieb wird der Stand des Aufrufers uebergeben, damit die
// Anzeige zwischen zwei Serverabrufen lokal weitertickt.
function secondsSince(value: string | null | undefined, bis: number | null): number | null {
  if (!value) return null
  const parsed = new Date(value).getTime()
  if (Number.isNaN(parsed)) return null
  return Math.max(0, Math.round(((bis ?? Date.now()) - parsed) / 1000))
}

// Alter der letzten Fortschrittsmeldung: zeigt, dass die Anzeige wirklich lebt.
function ageSeconds(value: string | null | undefined, now: number): number | null {
  if (!value) return null
  const parsed = new Date(value).getTime()
  if (Number.isNaN(parsed)) return null
  return Math.max(0, Math.round((now - parsed) / 1000))
}

function durationLabel(seconds: number): string {
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}:${String(seconds % 60).padStart(2, '0')} Min`
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')} Std`
}

// Kurze Auskunft zur Titelprüfung des letzten Laufs. Sie erklärt, warum
// Angebote fehlen.
function titleCheckLine(run: Run): string | null {
  const check = run.progress?.title_check
  if (!check || check.geprueft === 0) return null
  // Die häufigsten Gründe nennen: "anderes Modell" zeigt, dass die Quelle
  // ähnliche statt passende Artikel geliefert hat - das ist die Erklärung,
  // wenn die Ergebnisliste leer bleibt.
  const gruende = (check.gruende ?? [])
    .filter((entry) => entry.anzahl > 0)
    .slice(0, 3)
    .map((entry) => `${count(entry.anzahl)} ${entry.label}`)
  const zubehoer =
    gruende.length > 0
      ? `, davon ${gruende.join(', ')}`
      : (check.zubehoer ?? 0) > 0
        ? `, davon ${count(check.zubehoer ?? 0)} Teile/Zubehör`
        : ''
  return `Titelprüfung: ${count(check.geprueft)} Titel geprüft, ${count(
    check.passt_nicht,
  )} passten nicht${zubehoer}`
}

// Was die deterministische Auswahl aussortiert hat (ohne KI): Der Benutzer soll
// sehen, dass defekte Ware und Neuware gar nicht erst in der Liste landen - und
// dass der Preisrahmen der häufigste Grund ist, wenn nichts übrig bleibt.
function selectionLine(run: Run): string | null {
  const excluded = run.progress?.excluded
  if (!excluded) return null
  const teile: string[] = []
  if (excluded.defect) teile.push(`${count(excluded.defect)} als defekt/Ersatzteil`)
  if (excluded.new_item) teile.push(`${count(excluded.new_item)} als Neuware`)
  if (excluded.price) teile.push(`${count(excluded.price)} über dem Kauf-Limit oder darunter`)
  if (teile.length === 0) return null
  return `Aussortiert: ${teile.join(', ')}`
}

// Verkäuferstandort: Ort und PLZ, sofern die Quelle das hergibt.
function locationLabel(listing: Listing): string {
  const parts = [listing.postal_code, listing.location_text].filter(Boolean)
  return parts.join(' ')
}

// Ein Angebot = eine Zeile. Die ganze Zeile ist der Link zum Original.
function Offer({ listing }: { listing: Listing }) {
  const price = listing.total_price ?? listing.price
  return (
    <li className="offer">
      <a href={listing.url} target="_blank" rel="noreferrer noopener">
        <span
          className={listing.deal_score === null ? 'offer-score unscored' : 'offer-score'}
          title={verdictLabel(listing.verdict)}
        >
          {listing.deal_score === null ? '–' : Math.round(listing.deal_score)}
        </span>
        <span className="offer-main">
          <span className="offer-title">{listing.title || 'Angebot'}</span>
          <span className="offer-meta">
            {platformLabel(listing.platform)}
            {listing.published_at && ` · ${new Date(listing.published_at).toLocaleDateString('de-DE')}`}
            {locationLabel(listing) && ` · ${locationLabel(listing)}`}
            {listing.pickup_available ? ' · Abholung möglich' : ''}
          </span>
        </span>
        <span className="offer-price">{euro(price)}</span>
        <span className="offer-open">öffnen ↗</span>
      </a>
    </li>
  )
}
