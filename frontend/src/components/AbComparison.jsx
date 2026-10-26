// A/B: flat max-CPM bidding (control) vs pCTR expected-value bidding (treatment).
// Headline is effective CPC: treatment should win the same impressions for
// less by shading bids on low-pCTR contexts. CTR is near-equal by design
// (click probability depends on the auction context, which is assigned
// independent of the arm), so any win shows up as a cheaper cost per click.
//
// The headline is the backend's verdict, not the raw difference: on a few
// dozen clicks the observed difference is mostly noise, and colouring it as a
// win would claim a result the data cannot support.
const VERDICTS = {
  treatment_cheaper: {
    color: '#34d399',
    text: 'Significant: expected-value bidding pays less per click.',
  },
  treatment_dearer: {
    color: '#f87171',
    text: 'Significant: expected-value bidding pays more per click.',
  },
  inconclusive: {
    color: '#9ca3af',
    text: 'Not significant yet: the 95% interval still includes no difference. ' +
      'Every filled auction adds data.',
  },
  insufficient_data: {
    color: '#9ca3af',
    text: 'Not enough clicks in both arms to compare yet. Seed the demo or run auctions.',
  },
}

export default function AbComparison({ data }) {
  if (!data?.arms?.length) return null
  const { arms, comparison } = data

  const pct = (x) => `${(x * 100).toFixed(2)}%`
  const signed = (x) => `${x <= 0 ? '−' : '+'}${Math.abs(x * 100).toFixed(0)}%`
  const cents = (x) => (x == null ? '—' : `${x.toFixed(1)}¢`)
  const dollars = (x) => `$${(x / 100).toFixed(2)}`

  const verdict = VERDICTS[comparison?.verdict] || VERDICTS.insufficient_data
  const hasInterval = comparison?.change != null

  const label = { control: 'Control — flat max CPM', treatment: 'Treatment — EV bidding' }

  return (
    <div style={{ background: '#111827', borderRadius: 12, padding: 20 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <h3 style={{ color: '#f9fafb', margin: 0 }}>Bidding strategy A/B</h3>
        {hasInterval && (
          <span style={{ color: verdict.color, fontWeight: 700, fontSize: 14 }}>
            {signed(comparison.change)} cost per click
            <span style={{ fontWeight: 400, marginLeft: 8 }}>
              95% CI {signed(comparison.ci_low)} to {signed(comparison.ci_high)}
            </span>
          </span>
        )}
      </div>
      <p style={{ color: '#9ca3af', fontSize: 13, margin: '4px 0 6px' }}>
        Expected-value bidding (pCTR × value-per-click) vs a flat max-CPM bid,
        randomised per auction. Clicks are simulated from held-out CTR data the
        model never trained on.
      </p>
      <p style={{ color: verdict.color, fontSize: 13, margin: '0 0 16px' }}>
        {verdict.text}
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
          {arms.map((r) => (
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
