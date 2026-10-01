import { useEffect, useMemo, useState } from "react"
import { Activity, ArrowDownLeft, ArrowRight, ArrowRightLeft, ArrowUpRight, Check, ChevronLeft, ChevronRight, Clock3, Database, Gauge, LayoutDashboard, ListFilter, RefreshCw, ShieldCheck, Zap } from "lucide-react"
import { Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { CostOverview, costStatus, formatCost } from "@/components/cost-overview"
import { get, type Bucket, type Logs, type ServiceStatus, type Statistics } from "@/lib/api"

const number = new Intl.NumberFormat("zh-CN")
const compact = new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 })
const dateTime = (value: number) => new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
const tick = (value: number, bucket: string) => new Date(value * 1000).toLocaleString("zh-CN", bucket === "hour" ? { hour: "2-digit", minute: "2-digit", hour12: false } : { month: "numeric", day: "numeric" })
function uptime(seconds: number) {
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds / 3600) % 24, minutes = Math.floor(seconds / 60) % 60
  return days ? `${days} 天 ${hours} 小时` : hours ? `${hours} 小时 ${minutes} 分钟` : `${minutes} 分钟 ${Math.floor(seconds) % 60} 秒`
}

function Metric({ label, value, detail, icon: Icon, tone = "green" }: { label: string; value: string; detail: string; icon: typeof Activity; tone?: string }) {
  return <Card className="gap-3 border bg-white py-5 shadow-none">
    <CardHeader className="flex flex-row items-center justify-between px-5 pb-0">
      <CardDescription className="font-medium">{label}</CardDescription>
      <div className={`rounded-lg p-2 ${tone === "green" ? "bg-emerald-50 text-emerald-700" : tone === "blue" ? "bg-blue-50 text-blue-600" : "bg-amber-50 text-amber-700"}`}><Icon className="size-4" /></div>
    </CardHeader>
    <CardContent className="px-5"><div className="stat-number text-3xl font-semibold">{value}</div><p className="mt-2 text-xs text-muted-foreground">{detail}</p></CardContent>
  </Card>
}

export default function App() {
  const [status, setStatus] = useState<ServiceStatus | null>(null)
  const [stats, setStats] = useState<Statistics | null>(null)
  const [logs, setLogs] = useState<Logs | null>(null)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)
  const [refresh, setRefresh] = useState(0)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [bucket, setBucket] = useState<"hour" | "day">("hour")
  const [page, setPage] = useState(1)
  const [statusCode, setStatusCode] = useState("all")
  const [draftStart, setDraftStart] = useState("")
  const [draftEnd, setDraftEnd] = useState("")
  const [range, setRange] = useState({ start: "", end: "" })
  const [preset, setPreset] = useState("all")

  useEffect(() => {
    const controller = new AbortController()
    const timeParams = new URLSearchParams()
    if (range.start) timeParams.set("start", range.start)
    if (range.end) timeParams.set("end", range.end)
    const statsParams = new URLSearchParams(timeParams)
    statsParams.set("bucket", bucket)
    const logParams = new URLSearchParams(timeParams)
    logParams.set("page", String(page))
    logParams.set("page_size", "20")
    if (statusCode !== "all") logParams.set("status_code", statusCode)
    setLoading(true)
    Promise.all([
      get<ServiceStatus>("/api/admin/status", controller.signal),
      get<Statistics>(`/api/admin/stats?${statsParams}`, controller.signal),
      get<Logs>(`/api/admin/logs?${logParams}`, controller.signal),
    ]).then(([nextStatus, nextStats, nextLogs]) => {
      if (controller.signal.aborted) return
      setStatus(nextStatus); setStats(nextStats); setLogs(nextLogs); setError(""); setLastUpdated(Date.now())
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "无法连接管理 API。")
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [bucket, page, statusCode, range, refresh])

  useEffect(() => { const timer = setInterval(() => setRefresh(value => value + 1), 30000); return () => clearInterval(timer) }, [])

  const chart = useMemo(() => {
    const step = bucket === "hour" ? 3600 : 86400
    const series = stats?.series ?? []
    const now = Math.floor(Date.now() / 1000 / step) * step
    const last = range.end ? Math.floor(new Date(range.end).getTime() / 1000 / step) * step : now
    const first = range.start ? Math.floor(new Date(range.start).getTime() / 1000 / step) * step : Math.min(series[0]?.bucket_start ?? last, last - (bucket === "hour" ? 23 : 29) * step)
    const count = Math.max(1, Math.floor((last - first) / step) + 1)
    const existing = new Map(series.map(point => [point.bucket_start, point]))
    // A long custom range must show its entire history. Fill empty buckets for
    // normal ranges; use sparse boundaries for very long histories to bound UI work.
    const empty = (time: number): Bucket => ({ bucket_start: time, requests: 0, successes: 0, failures: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, costs: [] })
    if (count > 1500) {
      const sparse = new Map(existing)
      if (!sparse.has(first)) sparse.set(first, empty(first))
      if (!sparse.has(last)) sparse.set(last, empty(last))
      const times = [...sparse.keys()].sort((a, b) => a - b)
      for (let index = 1; index < times.length; index++) {
        if (times[index] - times[index - 1] > step) {
          sparse.set(times[index - 1] + step, empty(times[index - 1] + step))
          sparse.set(times[index] - step, empty(times[index] - step))
        }
      }
      return [...sparse.values()].sort((a, b) => a.bucket_start - b.bucket_start)
    }
    return Array.from({ length: count }, (_, index): Bucket => {
      const time = first + index * step
      return existing.get(time) ?? empty(time)
    })
  }, [stats, bucket, range.start, range.end])

  function applyRange() {
    const start = draftStart ? new Date(draftStart) : null, end = draftEnd ? new Date(draftEnd) : null
    if ((start && !Number.isFinite(start.getTime())) || (end && !Number.isFinite(end.getTime())) || (start && end && start > end)) { setError("请选择有效的时间范围，开始时间不能晚于结束时间。"); return }
    setPage(1); setPreset("custom"); setRange({ start: start?.toISOString() ?? "", end: end?.toISOString() ?? "" })
    if (start && (end?.getTime() ?? Date.now()) - start.getTime() > 14 * 86400000) setBucket("day")
  }

  function selectPreset(value: string) {
    const start = new Date()
    if (value === "today") start.setHours(0, 0, 0, 0)
    else start.setDate(start.getDate() - (value === "7d" ? 7 : 30))
    const localInput = new Date(start.getTime() - start.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
    setPreset(value); setPage(1); setDraftStart(value === "all" ? "" : localInput); setDraftEnd("")
    setRange({ start: value === "all" ? "" : start.toISOString(), end: "" })
    setBucket(value === "7d" || value === "30d" ? "day" : "hour")
  }

  const totals = stats?.totals
  const pageCount = Math.max(1, Math.ceil((logs?.total ?? 0) / 20))
  const running = status?.status === "running" && !error

  return <div className="min-h-screen lg:grid lg:grid-cols-[228px_1fr]">
    <aside className="flex flex-col border-b bg-white lg:sticky lg:top-0 lg:h-screen lg:border-r lg:border-b-0">
      <div className="flex items-center gap-3 px-6 py-7"><div className="rounded-xl bg-primary p-2.5 text-white"><ArrowRightLeft className="size-5" /></div><div><div className="text-lg font-semibold tracking-tight">AM2OAIR</div><div className="mt-0.5 text-[10px] font-medium tracking-[0.18em] text-muted-foreground">PROTOCOL RELAY</div></div></div>
      <nav aria-label="管理导航" className="flex gap-2 px-4 lg:flex-col"><a href="#overview" className="flex items-center gap-3 rounded-lg bg-accent px-4 py-3 text-sm font-medium text-primary"><LayoutDashboard className="size-4" />概览</a><a href="#cost-statistics" className="flex items-center gap-3 rounded-lg px-4 py-3 text-sm text-muted-foreground hover:bg-muted"><Gauge className="size-4" />费用统计</a><a href="#request-logs" className="flex items-center gap-3 rounded-lg px-4 py-3 text-sm text-muted-foreground hover:bg-muted"><Activity className="size-4" />调用日志</a></nav>
      <div className="mt-auto hidden p-5 lg:block"><div className="dashboard-grid rounded-xl border bg-emerald-50/40 p-4"><ShieldCheck className="mb-3 size-5 text-primary" /><p className="text-xs font-semibold">只记录必要的元数据</p><p className="mt-2 text-xs leading-5 text-muted-foreground">提示词、模型输出与密钥不会进入调用日志。</p></div><div className="mt-5 flex items-center justify-between text-[11px] text-muted-foreground"><span>本地部署</span><span>v0.1.0</span></div></div>
    </aside>

    <main id="overview" className="min-w-0">
      <header className="flex items-center justify-between border-b bg-white/80 px-5 py-4 md:px-9"><div className="flex items-center gap-2 text-xs text-muted-foreground">工作台 <ChevronRight className="size-3" /><span className="font-medium text-foreground">服务概览</span></div><Badge variant="outline" className="gap-2 bg-white font-normal"><span className={`size-1.5 rounded-full ${running ? "bg-emerald-500" : "bg-amber-500"}`} />{running ? "服务运行中" : loading ? "正在连接" : "连接异常"}</Badge></header>
      <div className="mx-auto max-w-[1560px] space-y-6 p-5 md:p-9">
        <div className="flex flex-wrap items-end justify-between gap-4"><div><p className="mb-2 text-xs font-medium tracking-[0.15em] text-primary">RELAY / OVERVIEW</p><h1 className="text-2xl font-semibold tracking-tight md:text-3xl">中继服务概览</h1><p className="mt-2 text-sm text-muted-foreground">按时间查看请求、Token 与估算费用，历史记录保存在本地 SQLite。</p></div><div className="flex items-center gap-3"><span className="hidden text-xs text-muted-foreground sm:inline">{lastUpdated ? `${new Date(lastUpdated).toLocaleTimeString("zh-CN", { hour12: false })} 更新` : "每 30 秒自动刷新"}</span><Button variant="outline" size="sm" onClick={() => setRefresh(value => value + 1)} disabled={loading}><RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />刷新</Button></div></div>

        {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}<span className="ml-2 text-xs">页面会自动重试。</span></div>}

        <Card className="gap-0 overflow-hidden border py-0 shadow-none"><CardContent className="grid gap-6 p-5 md:grid-cols-[1fr_auto] md:p-6"><div><div className="flex items-center gap-2"><span className="flex size-5 items-center justify-center rounded-full bg-emerald-50 text-primary"><Check className="size-3" /></span><span className="text-sm font-semibold">Responses → Anthropic Messages</span></div><div className="mt-4 flex flex-wrap items-center gap-2 text-xs"><span className="rounded-md border bg-muted/60 px-2.5 py-1.5 font-mono">Codex CLI</span><ArrowRight className="size-3 text-muted-foreground" /><span className="rounded-md border bg-muted/60 px-2.5 py-1.5 font-mono">127.0.0.1:8787</span><ArrowRight className="size-3 text-muted-foreground" /><span className="break-all rounded-md border bg-accent/50 px-2.5 py-1.5 font-mono text-primary">{status?.config.base_url ?? "—"}</span></div><div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">当前模型 <code className="break-all font-medium text-foreground">{status?.config.model ?? "—"}</code></div></div><div className="flex items-center gap-6 border-t pt-4 text-xs md:border-t-0 md:border-l md:pt-0 md:pl-6"><div><Clock3 className="mb-2 size-4 text-muted-foreground" /><p className="text-muted-foreground">运行时间</p><p className="mt-1.5 font-semibold">{status ? uptime(status.uptime_seconds) : "—"}</p></div><div><Database className="mb-2 size-4 text-muted-foreground" /><p className="text-muted-foreground">持久化存储</p><p className="mt-1.5 font-semibold">{status?.database === "ok" ? "SQLite · 正常" : "—"}</p></div></div></CardContent></Card>

        <Card className="gap-4 border py-5 shadow-none">
          <CardHeader className="flex flex-wrap flex-row items-center justify-between gap-3 px-5"><div><CardTitle className="flex items-center gap-2 text-sm"><Gauge className="size-4 text-primary" />统计时间</CardTitle><CardDescription className="mt-1 text-xs">费用、请求数、Token 与调用日志使用同一时间范围。</CardDescription></div><div className="flex flex-wrap gap-1">{[["all", "全部"], ["today", "今天"], ["7d", "近 7 天"], ["30d", "近 30 天"]].map(([value, label]) => <Button key={value} size="sm" variant={preset === value ? "default" : "outline"} onClick={() => selectPreset(value)}>{label}</Button>)}</div></CardHeader>
          <CardContent className="px-5"><div className="flex flex-wrap items-end gap-3">
            <div className="max-w-full"><label className="mb-1.5 block text-[11px] text-muted-foreground" htmlFor="time-start">开始时间（本地）</label><Input id="time-start" type="datetime-local" value={draftStart} onChange={event => setDraftStart(event.target.value)} className="h-9 w-[204px] text-xs" /></div>
            <div className="max-w-full"><label className="mb-1.5 block text-[11px] text-muted-foreground" htmlFor="time-end">结束时间（本地）</label><Input id="time-end" type="datetime-local" value={draftEnd} onChange={event => setDraftEnd(event.target.value)} className="h-9 w-[204px] text-xs" /></div>
            <Button variant="outline" size="sm" onClick={applyRange}><ListFilter className="size-3.5" />查询统计</Button>
            <div><label className="mb-1.5 block text-[11px] text-muted-foreground">趋势聚合方式</label><Select value={bucket} onValueChange={value => setBucket(value as "hour" | "day")}><SelectTrigger className="h-9 w-[108px] bg-white" aria-label="趋势聚合方式"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="hour">按小时</SelectItem><SelectItem value="day">按天</SelectItem></SelectContent></Select></div>
          </div><p className="mt-3 text-[11px] text-muted-foreground">当前查询：{range.start ? dateTime(new Date(range.start).getTime() / 1000) : "全部历史"} → {range.end ? dateTime(new Date(range.end).getTime() / 1000) : "现在"}</p></CardContent>
        </Card>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="请求总数" value={totals ? number.format(totals.requests) : "—"} detail={totals ? `${number.format(totals.successes)} 次成功 · ${number.format(totals.failures)} 次失败 / 未完成` : "正在读取持久化记录"} icon={Activity} />
          <Metric label="请求成功率" value={totals ? `${totals.success_rate.toFixed(1)}%` : "—"} detail="仅 completed 响应计为成功" icon={ShieldCheck} />
          <Metric label="输入 Token" value={totals ? compact.format(totals.input_tokens) : "—"} detail={totals ? `${number.format(totals.input_tokens)} token · 含缓存输入` : "—"} icon={ArrowDownLeft} tone="blue" />
          <Metric label="输出 Token" value={totals ? compact.format(totals.output_tokens) : "—"} detail={totals ? `总用量 ${number.format(totals.total_tokens)} token` : "—"} icon={ArrowUpRight} tone="amber" />
        </div>

        <CostOverview stats={stats} pricing={status?.pricing} chart={chart} bucket={bucket} />

        <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-sm font-semibold">请求与 Token 趋势</h2><span className="text-[11px] text-muted-foreground">按所选时间范围 · UTC 聚合 · 本地时间显示</span></div>
        <div className="grid gap-5 xl:grid-cols-2">
          <Card className="border shadow-none"><CardHeader className="flex flex-row justify-between pb-2"><div><CardTitle className="text-sm">请求量</CardTitle><CardDescription className="mt-1 text-xs">每个时间段的完成与异常请求</CardDescription></div><div className="flex items-start gap-3 pt-1 text-[10px] text-muted-foreground"><span><i className="mr-1 inline-block size-1.5 rounded-full bg-emerald-600" />成功</span><span><i className="mr-1 inline-block size-1.5 rounded-full bg-rose-400" />失败</span></div></CardHeader><CardContent className="h-[232px] px-2 pb-4 pr-5"><ResponsiveContainer width="100%" height="100%"><AreaChart data={chart} margin={{ top: 15, right: 10, bottom: 0, left: -22 }}><defs><linearGradient id="requestGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#198562" stopOpacity={0.18} /><stop offset="100%" stopColor="#198562" stopOpacity={0} /></linearGradient></defs><CartesianGrid stroke="#edf0f3" vertical={false} strokeDasharray="3 4" /><XAxis type="number" domain={["dataMin", "dataMax"]} dataKey="bucket_start" tickFormatter={value => tick(value, bucket)} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: "#8492a4" }} minTickGap={34} /><YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: "#8492a4" }} /><Tooltip labelFormatter={value => dateTime(Number(value))} contentStyle={{ borderRadius: 10, borderColor: "#e3e8ee", fontSize: 12 }} /><Area name="成功" type="monotone" dataKey="successes" stroke="#198562" strokeWidth={2} fill="url(#requestGradient)" isAnimationActive={false} /><Area name="失败 / 未完成" type="monotone" dataKey="failures" stroke="#e26a75" strokeWidth={1.5} fill="transparent" isAnimationActive={false} /></AreaChart></ResponsiveContainer></CardContent></Card>
          <Card className="border shadow-none"><CardHeader className="flex flex-row justify-between pb-2"><div><CardTitle className="text-sm">Token 用量</CardTitle><CardDescription className="mt-1 text-xs">输入、输出与总 token</CardDescription></div><Zap className="size-4 text-muted-foreground" /></CardHeader><CardContent className="h-[232px] px-2 pb-4 pr-5"><ResponsiveContainer width="100%" height="100%"><LineChart data={chart} margin={{ top: 15, right: 10, bottom: 0, left: -12 }}><CartesianGrid stroke="#edf0f3" vertical={false} strokeDasharray="3 4" /><XAxis type="number" domain={["dataMin", "dataMax"]} dataKey="bucket_start" tickFormatter={value => tick(value, bucket)} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: "#8492a4" }} minTickGap={34} /><YAxis tickFormatter={value => compact.format(value)} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: "#8492a4" }} /><Tooltip labelFormatter={value => dateTime(Number(value))} contentStyle={{ borderRadius: 10, borderColor: "#e3e8ee", fontSize: 12 }} /><Line name="输入 token" type="monotone" dataKey="input_tokens" stroke="#5288d2" strokeWidth={2} dot={false} isAnimationActive={false} /><Line name="输出 token" type="monotone" dataKey="output_tokens" stroke="#d7a343" strokeWidth={2} dot={false} isAnimationActive={false} /><Line name="总 token" type="monotone" dataKey="total_tokens" stroke="#198562" strokeWidth={1.5} dot={false} isAnimationActive={false} /></LineChart></ResponsiveContainer></CardContent></Card>
        </div>

        <Card id="request-logs" className="gap-0 overflow-hidden border py-0 shadow-none"><CardHeader className="gap-4 border-b p-5">
          <div className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle className="text-base">调用日志</CardTitle><CardDescription className="mt-1 text-xs">使用上方统计时间范围，单次费用依据请求时单价。</CardDescription></div><Badge variant="secondary" className="font-normal">{number.format(logs?.total ?? 0)} 条记录</Badge></div>
          <div className="flex items-end gap-3"><div><label className="mb-1.5 block text-[11px] text-muted-foreground" htmlFor="http-status">HTTP 状态码（仅筛选日志）</label><Select value={statusCode} onValueChange={value => { setPage(1); setStatusCode(value) }}><SelectTrigger id="http-status" className="h-9 w-[128px]"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">全部状态</SelectItem>{[200, 400, 401, 403, 413, 429, 500, 502, 503, 504].map(code => <SelectItem key={code} value={String(code)}>{code}</SelectItem>)}</SelectContent></Select></div><Button variant="ghost" size="sm" onClick={() => { setStatusCode("all"); setPage(1) }}>重置状态</Button></div>
        </CardHeader>
          <CardContent className="p-0"><Table><TableHeader><TableRow className="bg-muted/35 hover:bg-muted/35"><TableHead className="pl-5">时间 / 接口</TableHead><TableHead>模型</TableHead><TableHead>状态</TableHead><TableHead>耗时</TableHead><TableHead>模式</TableHead><TableHead>Token 输入 / 输出</TableHead><TableHead>估算费用</TableHead><TableHead>请求 ID</TableHead></TableRow></TableHeader><TableBody>{logs?.items.map(log => <TableRow key={log.id} className="text-xs"><TableCell className="py-4 pl-5"><div className="whitespace-nowrap font-medium">{dateTime(log.requested_at)}</div><div className="mt-1 text-[10px] text-muted-foreground">{log.endpoint}</div></TableCell><TableCell><span className="block max-w-[180px] truncate font-mono" title={log.model}>{log.model}</span></TableCell><TableCell><Badge variant="outline" className={log.success ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-rose-200 bg-rose-50 text-rose-700"}>{log.http_status}</Badge>{log.error_category && <div className="mt-1 max-w-[160px] truncate text-[10px] text-muted-foreground" title={log.error_category}>{log.error_category}</div>}</TableCell><TableCell className="whitespace-nowrap font-mono text-muted-foreground">{log.duration_ms >= 1000 ? `${(log.duration_ms / 1000).toFixed(2)} s` : `${log.duration_ms.toFixed(0)} ms`}</TableCell><TableCell><Badge variant="secondary" className="whitespace-nowrap font-normal">{log.stream ? "SSE 流式" : "JSON"}</Badge></TableCell><TableCell className="whitespace-nowrap font-mono"><span>{number.format(log.input_tokens)}</span><span className="mx-1 text-muted-foreground">/</span><span>{number.format(log.output_tokens)}</span><div className="mt-1 text-[10px] text-muted-foreground">共 {number.format(log.total_tokens)}</div></TableCell><TableCell className="whitespace-nowrap"><span className="font-mono">{formatCost(log.cost.total_cost, log.cost.currency)}</span><div className="mt-1 text-[10px] text-muted-foreground">{costStatus[log.cost.status] ?? "费用未知"}</div></TableCell><TableCell className="max-w-[250px] pr-5"><span className="block truncate font-mono text-[10px]" title={log.request_id}>{log.request_id}</span><span className="mt-1 block truncate font-mono text-[10px] text-muted-foreground" title={log.upstream_request_id ?? ""}>{log.upstream_request_id ?? "无上游 ID"}</span></TableCell></TableRow>)}{!logs?.items.length && <TableRow><TableCell colSpan={8} className="h-32 text-center text-sm text-muted-foreground">{loading ? "正在读取日志…" : error ? "日志暂时不可用" : "暂无符合条件的请求。发起一次中继调用后，记录会显示在这里。"}</TableCell></TableRow>}</TableBody></Table></CardContent>
          <div className="flex items-center justify-between border-t px-5 py-3 text-xs text-muted-foreground"><span>每页 20 条 · 第 {page} / {pageCount} 页</span><div className="flex gap-2"><Button variant="outline" size="icon" className="size-7" aria-label="上一页" disabled={page <= 1 || loading} onClick={() => setPage(value => value - 1)}><ChevronLeft className="size-3.5" /></Button><Button variant="outline" size="icon" className="size-7" aria-label="下一页" disabled={page >= pageCount || loading} onClick={() => setPage(value => value + 1)}><ChevronRight className="size-3.5" /></Button></div></div>
        </Card>
        <footer className="flex flex-wrap justify-between gap-2 text-[11px] text-muted-foreground"><span className="flex items-center gap-1.5"><ShieldCheck className="size-3" />请求与响应内容不持久化 · API Key 不显示</span><span>AM2OAIR / localhost:8787</span></footer>
      </div>
    </main>
  </div>
}
