// A/B: flat max-CPM bidding (control) vs pCTR expected-value bidding (treatment).
// Headline is effective CPC — treatment wins the same impressions for less by
// shading bids on low-pCTR contexts. CTR is near-equal by design (click
// probability depends on the auction context, which is assigned independent of
// the arm), so the win shows up as a cheaper cost per click, not a higher CTR.
export default function AbComparison({ data }) {
  if (!data || data.length === 0) return null

  const byArm = Object.fromEntries(data.map((r) => [r.strategy, r]))
  const control = byArm.control
  const treatment = byArm.treatment

  const pct = (x) => `${(x * 100).toFixed(2)}%`
  const cents = (x) => (x == null ? '—' : `${x.toFixed(1)}¢`)
  const dollars = (x) => `$${(x / 100).toFixed(2)}`

  // Effective CPC improvement (lower is better).
  let deltaLabel = null
  if (control?.eff_cpc_cents && treatment?.eff_cpc_cents) {
    const change = (treatment.eff_cpc_cents - control.eff_cpc_cents) / control.eff_cpc_cents
    deltaLabel = `${change <= 0 ? '−' : '+'}${Math.abs(change * 100).toFixed(0)}% cost per click`
  }

  const label = { control: 'Control — flat max CPM', treatment: 'Treatment — EV bidding' }

  return (
    <div style={{ background: '#111827', borderRadius: 12, padding: 20 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12 }}>
        <h3 style={{ color: '#f9fafb', margin: 0 }}>Bidding strategy A/B</h3>
        {deltaLabel && (
          <span style={{ color: '#34d399', fontWeight: 700, fontSize: 14 }}>{deltaLabel}</span>
        )}
      </div>
      <p style={{ color: '#9ca3af', fontSize: 13, margin: '4px 0 16px' }}>
        Expected-value bidding (pCTR × value-per-click) vs a flat max-CPM bid.
        The model wins the same impressions for a lower cost per click — CTR is
        near-equal by design, the gain is efficiency.
      </p>
      <table style={{ width: '100%', borderCollapse: 'collapse', color: '#e5e7eb' }}>
        <thead>
          <tr style={{ textAlign: 'left', color: '#9ca3af', fontSize: 12 }}>
            <th style={{ padding: '6px 0' }}>Arm</th>
            <th>Impressions</th><th>Clicks</th><th>CTR</th><th>Spend</th>
            <th style={{ color: '#e5e7eb' }}>Eff. CPC</th>
          </tr>
        </thead>
        <tbody>
          {data.map((r) => (
            <tr key={r.strategy} style={{ borderTop: '1px solid #1f2937' }}>
              <td style={{ padding: '8px 0' }}>{label[r.strategy] || r.strategy}</td>
              <td>{r.impressions}</td>
              <td>{r.clicks}</td>
              <td>{pct(r.ctr)}</td>
              <td>{dollars(r.spend_cents)}</td>
              <td style={{ fontWeight: 700 }}>{cents(r.eff_cpc_cents)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
