// Typen spiegeln die Backend-Schemas in backend/schemas.py wider.

export type DeliveryMode = 'shipping' | 'pickup' | 'both'

export interface SearchRequest {
  // Konkreter Produktname (nur noch für Aufrufer, die ihn direkt setzen).
  // Die Oberfläche sucht über die Kauf-Limit-Liste (siehe price_groups).
  product: string | null
  categories: string[]
  // Gruppen der Kauf-Limit-Liste (Höchstpreise.txt), z. B. "gpu" oder "cpu".
  // Je Komponente gilt ihr eigenes Limit als Preisgrenze.
  price_groups: string[]
  platforms: string[]
  max_price: number | null
  price_per_category: Record<string, number>
  delivery: DeliveryMode
  postal_code: string | null
  radius_km: number | null
  days_back: number
  min_condition_rank: number | null
  only_working: boolean
  seller_location: string | null
  min_deal_score: number
  min_price_advantage_pct: number | null
  only_new_since_last_run: boolean
  exclude_problematic: boolean
  preferred_models: string[]
  max_rounds: number | null
}

export interface Category {
  key: string
  label: string
  default_max_price: number
  base_terms: string[]
}

export interface PriceLimit {
  group: string
  group_label: string
  category: string
  name: string
  term: string
  max_price: number
}

export interface PriceLimitGroup {
  key: string
  label: string
  category: string
  count: number
  min_price: number
  max_price: number
}

export interface PriceList {
  available: boolean
  path: string
  loaded_at: string | null
  groups: PriceLimitGroup[]
  entries: PriceLimit[]
  errors: string[]
}

// Empfehlung für das Kauf-Limit einer Komponente, aus beobachteten Preisen
// abgeleitet (günstigstes Zehntel). Ohne Datenbasis bleibt sie aus.
export interface PriceLimitSuggestion {
  index: number
  group: string
  group_label: string
  category: string
  name: string
  term: string
  current_max_price: number
  suggested_max_price: number | null
  observations: number
  min_total: number | null
  q10: number | null
  median: number | null
  q75: number | null
  direction: string
  change_pct: number | null
  reason: string
}

export interface PriceLimitAdvice {
  generated_at: string
  path: string
  min_observations: number
  window_days: number
  target_quantile: number
  max_change_ratio: number
  components: number
  with_data: number
  changes: number
  suggestions: PriceLimitSuggestion[]
  summary: string
  disclaimer: string
  applied: number
  backup_path: string | null
}

export interface PlatformProgress {
  platform: string
  label: string
  queries_total: number
  queries_done: number
  results_returned: number
  results_usable: number
  errors: number
  status: string
}

export interface RunProgress {
  phase?: string
  phase_label?: string
  message?: string
  queries_total?: number
  queries_done?: number
  platforms?: PlatformProgress[]
  results_found?: number
  results_usable?: number
  results_matching_filters?: number
  new_listings?: number
  updated_listings?: number
  raw_hits?: number
  raw_hits_usable?: number
  duplicates?: number
  excluded?: Record<string, number>
  scored?: number
  details_fetched?: number
  // Titelprüfung des Laufs: geprüfte, passende und verworfene Titel.
  title_check?: {
    aktiv: boolean
    geprueft: number
    passt: number
    passt_nicht: number
    // Teile und Zubehör, die die Schlagwort-Regel verworfen hat.
    zubehoer?: number
    // Angebote, bei denen erst die Beschreibung die Zuordnung gerettet hat.
    aus_beschreibung?: number
    // Verteilung der Gründe (z.B. "anderes Modell", "fremdes Gerät").
    gruende?: { art: string; label: string; anzahl: number }[]
  }
  rounds?: number
  plan_source?: string
  plan_source_label?: string
  plan_summary?: {
    total_queries: number
    expected_coverage?: string
    uncertainties?: string[]
    categories?: {
      category: string
      label: string
      queries: number
      detected_models: number
      families: string[]
    }[]
  }
  errors?: string[]
  limitations?: string[]
  paused?: boolean
  cancelled?: boolean
  started_at?: string | null
  updated_at?: string
  coverage?: Coverage
  coverage_statement?: string
}

export interface CoveragePlatform {
  platform: string
  label: string
  queries: number
  successful: number
  failed: number
  skipped: number
  results_returned: number
  results_usable: number
  new_listings: number
  duplicates: number
  excluded_auctions: number
  excluded_unknown_format: number
  excluded_price: number
  excluded_title_filter: number
  offset_reached: number
  reported_total_sum: number
  queries_with_total: number
  truncated_queries: number
  limits_reached: string[]
  errors: string[]
  coverage_ratio: number | null
  coverage_label: string
}

export interface Coverage {
  generated_at: string
  platforms: CoveragePlatform[]
  totals: Record<string, number>
  not_retrievable: string[]
  estimate_reliable: boolean
  fully_searched: boolean
  fully_searched_reason: string
  notes: string[]
}

export interface Run {
  id: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  status: string
  request: SearchRequest
  plan: unknown
  plan_source: string | null
  progress: RunProgress | null
  coverage: Coverage | null
  errors: string[] | null
  limitations: unknown[] | null
  queries_total: number
  queries_done: number
  result_count: number
  new_count: number
  updated_count: number
}

export interface RunSummary {
  id: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  status: string
  queries_total: number
  queries_done: number
  result_count: number
  new_count: number
  updated_count: number
  plan_source: string | null
}

export interface Listing {
  id: number
  platform: string
  url: string
  title: string
  price: number | null
  shipping_cost: number | null
  shipping_cost_known: boolean
  total_price: number | null
  currency: string
  market_value: number | null
  price_advantage_abs: number | null
  price_advantage_pct: number | null
  deal_score: number | null
  confidence: number | null
  verdict: string | null
  category: string | null
  manufacturer: string | null
  model: string | null
  variant: string | null
  condition_text: string | null
  condition_rank: number | null
  published_at: string | null
  published_at_known: boolean
  fetched_at: string
  last_seen_at: string
  location_text: string | null
  postal_code: string | null
  seller_name: string | null
  seller_feedback_percent: number | null
  listing_format: string
  pickup_available: boolean
  status: string
  image_url: string | null
  is_favorite: boolean
  reasons: string[] | null
  warnings: string[] | null
  market_uncertain: boolean
  market_method: string | null
}

export interface PricePoint {
  observed_at: string
  price: number | null
  shipping_cost: number | null
  total_price: number | null
  status: string | null
}

export interface ListingDetail extends Listing {
  description: string | null
  format_evidence: string | null
  raw: Record<string, unknown> | null
  market_basis: Record<string, unknown> | null
  score_breakdown: ScoreBreakdown | null
  model_candidates: Record<string, unknown>[] | null
  price_history: PricePoint[]
}

export interface ScoreComponent {
  key: string
  label: string
  weight: number
  subscore: number
  contribution: number
  explanation: string
}

export interface ScoreBreakdown {
  score: number
  base_score: number
  ceiling: number | null
  confidence: number
  verdict: string
  verdict_label: string
  risk_score: number
  price_advantage_abs: number | null
  price_advantage_pct: number | null
  market_value: number | null
  components: ScoreComponent[]
  reasons: string[]
  warnings: string[]
  assessment_source?: string
  market_basis?: Record<string, unknown>
}

export interface ListingList {
  items: Listing[]
  total: number
  limit: number
  offset: number
  sort: string
}

// Ergebnis einer Bestandsprüfung (Titelprüfung über alles Gespeicherte).
export interface RevalidationResult {
  ok: boolean
  summary: string
  geprueft: number
  aussortiert: number
  geschont: number
  // Allein über die Beschreibung dem Suchauftrag zugeordnet.
  gerettet: number
  gruende: { art: string; label: string; anzahl: number }[]
  beispiele: { listing_id: number; title: string; art: string; art_label: string; reason: string }[]
}

export interface FilterOptions {
  platforms: { value: string; count: number }[]
  models: { category: string; model: string; count: number }[]
  verdicts: { value: string; count: number }[]
  conditions: { rank: number; label: string }[]
  runs: { id: number; created_at: string | null }[]
  knowledge_categories: string[]
}

export interface ConnectorCapabilities {
  platform: string
  label: string
  enabled: boolean
  configured: boolean
  available: boolean
  supports: Record<string, boolean>
  page_size_max: number | null
  max_results_per_query: number | null
  max_results_per_result_set: number | null
  limitations: string[]
  docs_url: string | null
  requires_credentials: boolean
  setup_hint: string | null
  reason_unavailable: string | null
}

export interface ConnectorTest {
  platform: string
  ok: boolean
  message: string
  details: Record<string, unknown>
}

export interface ConnectorNotice {
  platform: string
  code: string
  message: string
  hint?: string | null
}

export interface ScoreWeights {
  price_advantage: number
  price_performance: number
  data_quality: number
  condition_risk: number
  total_cost: number
  freshness: number
  knowledge_match: number
  location: number
  market_comparison: number
  information_quality: number
}

export interface RuntimeSettings {
  exclude_auctions: boolean
  exclude_unknown_format: boolean
  allow_mixed_auction_bin: boolean
  search_max_variants_per_category: number
  search_max_parallel_requests: number
  search_max_pages_per_query: number
  search_parallel_pages_per_query: number
  search_max_queries_per_run: number
  search_max_rounds: number
  search_max_detail_fetches: number
  default_max_price_per_category: Record<string, number>
  default_days_back: number
  default_platforms: string[]
  default_min_deal_score: number
  default_min_condition_rank: number
  weights: ScoreWeights
  market_min_observations: number
  market_reference_window_days: number
  retention_days: number
  log_level: string
}

export interface SettingsPayload {
  runtime: RuntimeSettings
  environment: Record<string, unknown>
  connector_notices: ConnectorNotice[]
}

export interface KnowledgeSummary {
  entries: number
  identity_only_entries: number
  priced_entries: number
  sources: string[]
  per_category: Record<string, number>
  loaded_at: string
  paths: string[]
  errors: string[]
  markdown_notes: { name: string; content: string }[]
}

export interface KnowledgeEntry {
  category: string
  model: string
  manufacturer: string | null
  product_family: string | null
  aliases: string[]
  model_numbers: string[]
  variants: string[]
  currency: string
  good_price_max: number | null
  fair_price_max: number | null
  overpriced_above: number | null
  performance_index: number | null
  known_issues: string[]
  risk_flags: string[]
  notes: string[]
  problematic: boolean
  exclude: boolean
  source: string
  version: string | null
  weight: number
}

export interface SystemStatus {
  database: { url: string; search_runs: number }
  listings: { total: number; active: number; gone: number; unknown: number }
  knowledge: KnowledgeSummary
  connectors: ConnectorCapabilities[]
  version: string
}

export interface BenchmarkCase {
  name: string
  description: string
  score: number | null
  verdict: string | null
  verdict_label: string | null
  confidence: number | null
  price_advantage_pct: number | null
  expectation: string
  passed: boolean
  failures: string[]
}

export interface Benchmark {
  cases: BenchmarkCase[]
  passed: number
  failed: number
  notes: string[]
}

export interface SearchQueryRow {
  id: number
  platform: string
  category: string | null
  term: string
  family: string | null
  // Suchansicht der Plattform (z.B. "newest" bei eBay), leer = Standardsicht.
  sort: string | null
  round_index: number
  status: string
  pages_fetched: number
  offset_reached: number
  reported_total: number | null
  results_returned: number
  results_usable: number
  excluded_auctions: number
  excluded_unknown_format: number
  excluded_price: number
  excluded_title_filter: number
  new_listings: number
  duplicates: number
  truncated: boolean
  error: string | null
}

export interface Explain {
  available: boolean
  source: string
  message?: string | null
  summary?: string | null
  main_arguments: string[]
  concerns: string[]
  open_questions: string[]
  suggested_comparisons: string[]
}

export interface StatsOverview {
  listings: { total: number; active: number; gone: number; unknown: number }
  best_score: number | null
  best_listing_id: number | null
  new_today: number
  generated_at: string
}
