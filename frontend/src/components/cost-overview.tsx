import { useState } from "react"
import { CircleDollarSign, Info } from "lucide-react"
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import type { Bucket, Pricing, Statistics } from "@/lib/api"

const moneyFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 9 })
export function formatCost(amount: string | null | undefined, currency: string | null | undefined) {
  return amount == null ? "—" : `${currency ?? ""} ${moneyFormat.format(Number(amount))}`
}

export const costStatus: Record<string, string> = {
  historical: "历史费用未知",
  missing_usage: "未收到用量",
  invalid_usage: "用量格式异常",
  unconfigured: "单价未配置",
  partial: "仅含已收到的用量",
  calculated: "按请求时单价估算",
  cost_overflow: "费用超出计算范围",
}

export function CostOverview({ stats, pricing, chart, bucket }: {
  stats: Statistics | null
  pricing: Pricing | undefined
  chart: Bucket[]
  bucket: "hour" | "day"
}) {
  const [selectedCurrency, setSelectedCurrency] = useState("")
  const currencies = Array.from(new Set([pricing?.currency ?? "USD", ...(stats?.costs.currencies.map(item => item.currency) ?? [])]))
  const currency = currencies.includes(selectedCurrency) ? selectedCurrency : currencies[0]
  const amounts = stats?.costs.currencies.find(item => item.currency === currency)
  const noRequests = stats?.totals.requests === 0 && pricing?.configured
  const costChart = chart.map(point => {
    const known = point.costs.find(item => item.currency === currency)
    const priced = point.costs.reduce((total, item) => total + item.priced_requests, 0)
    return { ...point, cost: known ? Number(known.total_cost) : point.requests === priced ? 0 : null }
  })
  const available = Boolean(amounts || noRequests)
  const breakdown = [
    ["普通输入", amounts?.input_cost],
    ["输出", amounts?.output_cost],
    ["缓存读取", amounts?.cache_read_cost],
    ["缓存写入", amounts?.cache_write_cost],
  ]
  const rateLabels: Record<keyof Pricing["rates"], string> = {
    input: "输入", output: "输出", cache_read: "缓存读取", cache_write: "缓存写入（未细分）", cache_write_5m: "缓存写入 · 5m", cache_write_1h: "缓存写入 · 1h",
  }

  return <Card id="cost-statistics" className="gap-0 overflow-hidden border py-0 shadow-none">
    <CardHeader className="flex flex-wrap flex-row items-center justify-between gap-3 border-b px-5 py-4">
      <div className="flex items-center gap-2"><CircleDollarSign className="size-4 text-primary" /><CardTitle className="text-sm">费用统计 <span className="font-normal text-muted-foreground">/ Cost</span></CardTitle><Badge variant="secondary" className="font-normal">估算</Badge></div>
      {currencies.length > 1 ? <Select value={currency} onValueChange={setSelectedCurrency}><SelectTrigger aria-label="费用币种" className="h-8 w-28"><SelectValue /></SelectTrigger><SelectContent>{currencies.map(value => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectContent></Select> : <span className="text-xs text-muted-foreground">{currency}</span>}
    </CardHeader>
    <CardContent className="grid gap-6 p-5 xl:grid-cols-[1fr_1fr]">
      <div>
        <p className="text-xs text-muted-foreground">所选时间范围内已估算的费用</p>
        <div className="mt-2 break-all text-3xl font-semibold tracking-tight tabular-nums" data-testid="cost-total">{formatCost(amounts?.total_cost ?? (noRequests ? "0" : null), currency)}</div>
        <p className="mt-2 text-xs text-muted-foreground">{stats ? `${stats.costs.priced_requests} 次已估算 · ${stats.costs.unpriced_requests} 次费用未知` : "正在读取费用统计"}{!!stats?.costs.partial_requests && ` · ${stats.costs.partial_requests} 次仅有部分用量`}</p>
        <div className="mt-5 grid grid-cols-2 gap-4">{breakdown.map(([label, amount]) => <div key={label}><p className="text-[11px] text-muted-foreground">{label}</p><p className="mt-1 font-mono text-sm">{formatCost(amount ?? (noRequests ? "0" : null), currency)}</p></div>)}</div>
        <div className="mt-5 flex items-start gap-2 rounded-lg bg-muted/60 p-3 text-xs leading-5 text-muted-foreground"><Info className="mt-0.5 size-3.5 shrink-0" /><p>{!pricing?.configured ? "单价未配置。请在 .env 设置输入、输出及所需缓存单价后重新创建容器。" : "费用根据上游 token 用量与请求时的配置单价估算，不包含账单折扣、税费或其他附加费。"} 历史记录或缺少单价的记录不计入费用合计，也不按 0 元处理。</p></div>
      </div>
      <div className="min-w-0">
        <CardDescription className="mb-3 text-xs">费用趋势 · {currency} / {bucket === "hour" ? "小时" : "天"} · 未知费用留空</CardDescription>
        <div className="h-[228px]">
          {available ? <ResponsiveContainer width="100%" height="100%"><AreaChart data={costChart} margin={{ top: 12, right: 8, bottom: 0, left: 0 }}>
            <defs><linearGradient id="costGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#198562" stopOpacity={0.18} /><stop offset="100%" stopColor="#198562" stopOpacity={0} /></linearGradient></defs>
            <CartesianGrid stroke="#edf0f3" vertical={false} strokeDasharray="3 4" />
            <XAxis type="number" domain={["dataMin", "dataMax"]} dataKey="bucket_start" tickFormatter={value => new Date(Number(value) * 1000).toLocaleString("zh-CN", bucket === "hour" ? { month: "numeric", day: "numeric", hour: "2-digit", hour12: false } : { month: "numeric", day: "numeric" })} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: "#8492a4" }} minTickGap={38} />
            <YAxis width={70} tickFormatter={value => moneyFormat.format(Number(value))} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: "#8492a4" }} />
            <Tooltip labelFormatter={value => new Date(Number(value) * 1000).toLocaleString("zh-CN", { hour12: false })} formatter={value => [formatCost(String(value), currency), "已估算费用"]} contentStyle={{ borderRadius: 10, borderColor: "#e3e8ee", fontSize: 12 }} />
            <Area type="linear" dataKey="cost" stroke="#198562" strokeWidth={2} fill="url(#costGradient)" connectNulls={false} isAnimationActive={false} />
          </AreaChart></ResponsiveContainer> : <div className="flex h-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed text-xs text-muted-foreground"><CircleDollarSign className="size-6 text-muted-foreground/60" /><span>{pricing?.configured ? "所选时间内没有可估算的费用" : "配置单价后显示费用趋势"}</span><span className="text-[11px]">时间筛选同时作用于费用、Token 与调用日志</span></div>}
        </div>
      </div>
    </CardContent>
    {pricing && <details className="border-t px-5 py-3 text-xs"><summary className="cursor-pointer text-muted-foreground">当前单价 · {pricing.currency} / 100 万 token</summary><div className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-[11px] text-muted-foreground">{Object.entries(pricing.rates).map(([key, rate]) => <span key={key}>{rateLabels[key as keyof Pricing["rates"]]} <strong className="ml-1 font-mono font-medium text-foreground">{rate ?? "未配置"}</strong></span>)}</div></details>}
  </Card>
}
