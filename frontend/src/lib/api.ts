export interface ServiceStatus {
  status: string
  started_at: number
  uptime_seconds: number
  config: { base_url: string; model: string }
  database: string
  schema_version: number
  pricing: Pricing
}

export interface Pricing {
  currency: string
  unit: string
  configured: boolean
  rates: Record<"input" | "output" | "cache_read" | "cache_write" | "cache_write_5m" | "cache_write_1h", string | null>
}

export interface CostAmounts {
  currency: string | null
  input_cost: string | null
  output_cost: string | null
  cache_read_cost: string | null
  cache_write_cost: string | null
  total_cost: string | null
}
export interface CostTotals extends CostAmounts {
  currency: string
  priced_requests: number
  partial_requests: number
}
export interface CostSummary {
  currencies: CostTotals[]
  priced_requests: number
  unpriced_requests: number
  partial_requests: number
}

export interface Totals {
  requests: number
  successes: number
  failures: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  success_rate: number
}

export interface Bucket extends Omit<Totals, "success_rate"> { bucket_start: number; costs: CostTotals[] }
export interface Statistics { totals: Totals; bucket: "hour" | "day"; timezone: string; series: Bucket[]; costs: CostSummary }
export interface LogRecord {
  id: number
  requested_at: number
  endpoint: string
  model: string
  stream: boolean
  http_status: number
  duration_ms: number
  request_id: string
  upstream_request_id: string | null
  input_tokens: number
  output_tokens: number
  total_tokens: number
  success: boolean
  error_category: string | null
  cost: CostAmounts & { status: string }
}
export interface Logs { items: LogRecord[]; total: number; page: number; page_size: number }

export async function get<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, cache: "no-store", credentials: "omit" })
  if (!response.ok) throw new Error(`管理 API 返回 HTTP ${response.status}，请检查服务状态与筛选条件。`)
  return response.json() as Promise<T>
}
