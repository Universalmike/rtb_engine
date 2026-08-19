import { useState } from 'react'
import { Zap, CheckCircle, XCircle } from 'lucide-react'
import { submitBidRequest } from '../api'

const COUNTRIES = ['US', 'GB', 'NG', 'DE', 'CA', 'AU', 'FR', 'BR']
const DEVICES = ['desktop', 'mobile', 'tablet']

// Reason codes returned on the bid response. Naming them is the whole point:
// a bidder count alone cannot tell you whether a rule fired or was ignored.
const EXCLUSION_LABELS = {
  country:        'Geo mismatch',
  device:         'Device mismatch',
  category:       'Category mismatch',
  domain:         'Domain not targeted',
  blocked_domain: 'Domain blocked',
  creative:       'No creative for size',
  floor:          'Could not clear floor',
}

// How the page's content category was arrived at. Shown because an inferred
// category that cannot be seen is indistinguishable from a guess.
const CATEGORY_SOURCE_LABELS = {
  section:      'from URL section',
  known_domain: 'known site',
  keyword:      'inferred from name',
  publisher:    "publisher's own",
  unknown:      'not recognised',
}

export default function BidSimulator({ slots, onAuctionComplete }) {
  const [form, setForm] = useState({
    ad_slot_id: '',
    country: 'US',
    device_type: 'desktop',
    page_url: 'https://example.com/article',
  })
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const handleSubmit = async () => {
    if (!form.ad_slot_id) { setError('Select an ad slot first'); return }
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const res = await submitBidRequest(form)
      setResult(res)
      onAuctionComplete?.()
    } catch (e) {
      setError(e.response?.data?.detail || 'Auction failed — is the backend running?')
    } finally {
      setLoading(false)
    }
  }

  const inputStyle = {
    background: 'var(--surface2)', border: '1px solid var(--border)',
    borderRadius: 6, padding: '8px 12px', color: 'var(--text)',
    fontSize: 13, width: '100%', fontFamily: 'inherit',
  }

  const labelStyle = { fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase',
    letterSpacing: '0.06em', marginBottom: 4, display: 'block' }

  return (
    <div style={{
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius)', padding: 20,
    }}>
      <div style={{ fontWeight: 600, marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
        <Zap size={16} color="var(--accent)" /> Run Auction
      </div>

      <div style={{ display: 'grid', gap: 12 }}>
        <div>
          <label style={labelStyle}>Ad Slot</label>
          <select style={inputStyle} value={form.ad_slot_id}
            onChange={e => {
              const slot = slots.find(s => s.id === e.target.value)
              // The page belongs to the publisher that owns the slot, so
              // default to a URL on their domain. Still editable: overriding
              // it is how you watch domain targeting bite.
              setForm(f => ({
                ...f,
                ad_slot_id: e.target.value,
                page_url: slot?.publisher_domain
                  ? `https://${slot.publisher_domain}/article/sample`
                  : f.page_url,
              }))
            }}>
            <option value="">Select a slot…</option>
            {slots.map(s => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.width}×{s.height})
                {s.publisher_domain ? ` — ${s.publisher_domain}` : ''}
              </option>
            ))}
          </select>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <div>
            <label style={labelStyle}>Country</label>
            <select style={inputStyle} value={form.country}
              onChange={e => setForm(f => ({ ...f, country: e.target.value }))}>
              {COUNTRIES.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label style={labelStyle}>Device</label>
            <select style={inputStyle} value={form.device_type}
              onChange={e => setForm(f => ({ ...f, device_type: e.target.value }))}>
              {DEVICES.map(d => <option key={d} value={d}>{d}</option>)}
            </select>
          </div>
        </div>

        <div>
          <label style={labelStyle}>Page URL</label>
          <input style={inputStyle} value={form.page_url}
            onChange={e => setForm(f => ({ ...f, page_url: e.target.value }))} />
          <div style={{ color: 'var(--muted)', fontSize: 11, marginTop: 4 }}>
            Any real site works — try espn.com, bloomberg.com, vogue.com or
            bbc.com/sport. The domain sets the page category that campaigns
            target.
          </div>
        </div>

        <button onClick={handleSubmit} disabled={loading} style={{
          background: loading ? 'var(--border)' : 'var(--accent)',
          color: '#fff', padding: '10px 0', borderRadius: 6,
          fontWeight: 600, fontSize: 14, transition: 'background 0.2s',
        }}>
          {loading ? 'Running auction…' : '⚡ Submit Bid Request'}
        </button>

        {error && (
          <div style={{ color: 'var(--red)', fontSize: 13, padding: '8px 12px',
            background: 'rgba(239,68,68,0.1)', borderRadius: 6 }}>
            {error}
          </div>
        )}

        {result && (
          <div style={{
            background: 'var(--surface2)', border: `1px solid ${result.had_fill ? 'var(--green)' : 'var(--border)'}`,
            borderRadius: 8, padding: 16, fontSize: 13,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, marginBottom: 12 }}>
              {result.had_fill
                ? <><CheckCircle size={16} color="var(--green)" /> Auction filled</>
                : <><XCircle size={16} color="var(--red)" /> No fill — no campaign bid</>}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              {[
                ['Bidders', result.num_bidders],
                ['Top bid', result.highest_bid_cents > 0 ? `$${(result.highest_bid_cents/100).toFixed(4)} CPM` : '—'],
                ['Clearing price', result.clearing_price_cents > 0 ? `$${(result.clearing_price_cents/100).toFixed(4)} CPM` : '—'],
                ['Impression cost', result.charged_cost_micros > 0
                  ? `$${(result.charged_cost_micros / 1_000_000).toFixed(6)}`
                  : '$0.000000'],
                ['Page category', result.page_category
                  ? `${result.page_category} · ${CATEGORY_SOURCE_LABELS[result.page_category_source] || result.page_category_source}`
                  : '—'],
                ['Latency', `${result.latency_ms}ms`],
                ['Auction type', result.auction_type?.replace('_', ' ')],
                ['Auction ID', result.auction_id.slice(0, 8) + '…'],
              ].map(([k, v]) => (
                <div key={k}>
                  <div style={{ color: 'var(--muted)', fontSize: 11, marginBottom: 2 }}>{k}</div>
                  <div style={{ fontWeight: 600 }}>{v}</div>
                </div>
              ))}
            </div>

            {result.excluded && Object.keys(result.excluded).length > 0 && (
              <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
                <div style={{ ...labelStyle, marginBottom: 8 }}>
                  Why the others did not bid
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {Object.entries(result.excluded)
                    .sort((a, b) => b[1] - a[1])
                    .map(([reason, count]) => (
                      <span key={reason} style={{
                        background: 'var(--surface)', border: '1px solid var(--border)',
                        borderRadius: 999, padding: '3px 10px', fontSize: 12,
                      }}>
                        {EXCLUSION_LABELS[reason] || reason}
                        <strong style={{ marginLeft: 6 }}>{count}</strong>
                      </span>
                  ))}
                </div>
                <div style={{ color: 'var(--muted)', fontSize: 11, marginTop: 8 }}>
                  Campaigns out of budget or paused are filtered before the
                  auction and are not counted here.
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
