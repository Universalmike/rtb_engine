function ProgressBar({ value, max, color = 'var(--accent)' }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0
  return (
    <div style={{ background: 'var(--border)', borderRadius: 4, height: 6, width: '100%' }}>
      <div style={{
        width: `${pct}%`, height: '100%', borderRadius: 4,
        background: pct > 85 ? 'var(--red)' : pct > 60 ? 'var(--amber)' : color,
        transition: 'width 0.4s ease',
      }} />
    </div>
  )
}

export default function CampaignTable({ campaigns, allCampaigns }) {
  // Merge stats with campaign details
  const merged = campaigns.map(stat => {
    const detail = allCampaigns.find(c => c.id === stat.campaign_id) || {}
    return { ...stat, ...detail }
  })

  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius)',
      overflow: 'hidden',
    }}>
      <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border)', fontWeight: 600 }}>
        Campaign Performance
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', minWidth: 640, borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ background: 'var(--surface2)' }}>
              {['Campaign', 'Status', 'Impressions', 'Clicks', 'CTR', 'Spend', 'Daily Budget'].map(h => (
                <th key={h} style={{
                  padding: '10px 16px', textAlign: 'left',
                  fontSize: 11, color: 'var(--muted)', fontWeight: 600,
                  textTransform: 'uppercase', letterSpacing: '0.06em',
                  whiteSpace: 'nowrap',
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {merged.length === 0 && (
              <tr>
                <td colSpan={7} style={{ padding: 32, textAlign: 'center', color: 'var(--muted)' }}>
                  No campaign data yet
                </td>
              </tr>
            )}
            {merged.map((c, i) => (
              <tr key={c.campaign_id} style={{
                borderTop: '1px solid var(--border)',
                background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.01)',
              }}>
                <td style={{ padding: '12px 16px', maxWidth: 200 }}>
                  <div style={{ fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {c.campaign_name}
                  </div>
                </td>
                <td style={{ padding: '12px 16px' }}>
                  <span style={{
                    fontSize: 11, padding: '3px 8px', borderRadius: 20, fontWeight: 600,
                    background: c.status === 'active' ? 'rgba(34,197,94,0.15)' :
                                c.status === 'exhausted' ? 'rgba(239,68,68,0.15)' : 'rgba(124,130,153,0.15)',
                    color: c.status === 'active' ? 'var(--green)' :
                           c.status === 'exhausted' ? 'var(--red)' : 'var(--muted)',
                  }}>
                    {c.status || 'active'}
                  </span>
                </td>
                <td style={{ padding: '12px 16px', fontWeight: 600 }}>
                  {c.impressions.toLocaleString()}
                </td>
                <td style={{ padding: '12px 16px' }}>{c.clicks.toLocaleString()}</td>
                <td style={{ padding: '12px 16px', color: c.ctr > 2 ? 'var(--green)' : 'var(--text)' }}>
                  {c.ctr.toFixed(2)}%
                </td>
                <td style={{ padding: '12px 16px', color: 'var(--accent2)' }}>
                  ${((c.total_spend_cents || 0) / 100).toFixed(2)}
                </td>
                <td style={{ padding: '12px 16px', minWidth: 140 }}>
                  <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
                    ${((c.spent_today_cents || 0) / 100).toFixed(2)} / ${((c.daily_budget_cents || 0) / 100).toFixed(2)}
                  </div>
                  <ProgressBar value={c.spent_today_cents || 0} max={c.daily_budget_cents || 1} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
