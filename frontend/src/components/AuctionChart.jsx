import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend
} from 'recharts'

function formatMinute(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`
}

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div style={{
      background: 'var(--surface2)', border: '1px solid var(--border)',
      borderRadius: 8, padding: '10px 14px', fontSize: 12,
    }}>
      <div style={{ color: 'var(--muted)', marginBottom: 6 }}>{formatMinute(label)}</div>
      {payload.map(p => (
        <div key={p.name} style={{ color: p.color, marginBottom: 2 }}>
          {p.name}: <strong>{p.value}</strong>
        </div>
      ))}
    </div>
  )
}

export default function AuctionChart({ data }) {
  const formatted = data.map(d => ({
    ...d,
    fill_rate: d.auctions > 0 ? Math.round((d.fills / d.auctions) * 100) : 0,
  }))

  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius)',
      padding: '20px',
    }}>
      <div style={{ fontWeight: 600, marginBottom: 16 }}>Auction Volume — Last 60 Minutes</div>
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={formatted} margin={{ top: 5, right: 10, left: -20, bottom: 0 }}>
          <defs>
            <linearGradient id="aGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3} />
              <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="fGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#22c55e" stopOpacity={0.3} />
              <stop offset="95%" stopColor="#22c55e" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
          <XAxis dataKey="minute" tickFormatter={formatMinute}
            tick={{ fill: 'var(--muted)', fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fill: 'var(--muted)', fontSize: 11 }} tickLine={false} axisLine={false} />
          <Tooltip content={<CustomTooltip />} />
          <Legend
            wrapperStyle={{ fontSize: 12, color: 'var(--muted)', paddingTop: 8 }}
          />
          <Area type="monotone" dataKey="auctions" name="Total Auctions"
            stroke="#6366f1" fill="url(#aGrad)" strokeWidth={2} dot={false} />
          <Area type="monotone" dataKey="fills" name="Filled"
            stroke="#22c55e" fill="url(#fGrad)" strokeWidth={2} dot={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
