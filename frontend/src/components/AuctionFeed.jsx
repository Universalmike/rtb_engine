import { CheckCircle, XCircle } from 'lucide-react'

function fmt(cents) {
  return cents > 0 ? `$${(cents / 100).toFixed(3)}` : '—'
}

function timeAgo(isoStr) {
  const diff = Math.floor((Date.now() - new Date(isoStr)) / 1000)
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  return `${Math.floor(diff / 3600)}h ago`
}

export default function AuctionFeed({ auctions }) {
  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius)',
      overflow: 'hidden',
    }}>
      <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border)', fontWeight: 600 }}>
        Live Auction Feed
        <span style={{
          marginLeft: 10, fontSize: 11, background: 'var(--accent)', color: '#fff',
          padding: '2px 8px', borderRadius: 20, fontWeight: 500,
        }}>LIVE</span>
      </div>

      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ background: 'var(--surface2)' }}>
              {['Status', 'Auction ID', 'Bidders', 'Top Bid', 'Clearing Price', 'Time'].map(h => (
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
            {auctions.length === 0 && (
              <tr>
                <td colSpan={6} style={{ padding: 32, textAlign: 'center', color: 'var(--muted)' }}>
                  No auctions yet — click "Run Auction" to start
                </td>
              </tr>
            )}
            {auctions.map((a, i) => (
              <tr key={a.auction_id} style={{
                borderTop: '1px solid var(--border)',
                background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.01)',
                transition: 'background 0.15s',
              }}>
                <td style={{ padding: '10px 16px' }}>
                  {a.had_fill
                    ? <CheckCircle size={15} color="var(--green)" />
                    : <XCircle size={15} color="var(--red)" />}
                </td>
                <td style={{ padding: '10px 16px', fontFamily: 'monospace', fontSize: 12, color: 'var(--muted)' }}>
                  {a.auction_id.slice(0, 8)}…
                </td>
                <td style={{ padding: '10px 16px', fontWeight: 600 }}>{a.num_bidders}</td>
                <td style={{ padding: '10px 16px', color: 'var(--accent2)' }}>{fmt(a.highest_bid_cents)}</td>
                <td style={{ padding: '10px 16px', color: 'var(--green)', fontWeight: 600 }}>
                  {fmt(a.clearing_price_cents)}
                </td>
                <td style={{ padding: '10px 16px', color: 'var(--muted)', fontSize: 12 }}>
                  {a.created_at ? timeAgo(a.created_at) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
