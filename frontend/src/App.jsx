import { useState, useEffect, useCallback } from 'react'
import { Activity, Database, Users, TrendingUp, RefreshCw, Loader } from 'lucide-react'
import StatCard from './components/StatCard'
import AuctionChart from './components/AuctionChart'
import AuctionFeed from './components/AuctionFeed'
import CampaignTable from './components/CampaignTable'
import BidSimulator from './components/BidSimulator'
import {
  fetchOverview, fetchCampaignStats, fetchTimeseries,
  fetchRecentAuctions, fetchAdSlots, fetchCampaigns, seedDatabase,
  API_URL_MISSING
} from './api'

function useInterval(fn, ms) {
  useEffect(() => {
    fn()
    const id = setInterval(fn, ms)
    return () => clearInterval(id)
  }, [])
}

// 'connecting' until the first successful load — a cold backend can take up to
// a minute to answer, and a visitor staring at empty cards assumes it's broken.
const STATUS = { CONNECTING: 'connecting', LIVE: 'live', ERROR: 'error' }

export default function App() {
  const [overview, setOverview]       = useState(null)
  const [campaignStats, setCStats]    = useState([])
  const [allCampaigns, setAllCamps]   = useState([])
  const [timeseries, setTimeseries]   = useState([])
  const [recentAuctions, setRecent]   = useState([])
  const [slots, setSlots]             = useState([])
  const [seeding, setSeeding]         = useState(false)
  const [seedMsg, setSeedMsg]         = useState(null)
  const [lastRefresh, setLastRefresh] = useState(null)
  const [status, setStatus]           = useState(STATUS.CONNECTING)
  const [errorDetail, setErrorDetail] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const [ov, cs, ts, ra, sl, ac] = await Promise.all([
        fetchOverview(),
        fetchCampaignStats(),
        fetchTimeseries(),
        fetchRecentAuctions(20),
        fetchAdSlots(),
        fetchCampaigns(),
      ])
      setOverview(ov)
      setCStats(cs)
      setTimeseries(ts)
      setRecent(ra)
      setSlots(sl)
      setAllCamps(ac)
      setLastRefresh(new Date())
      setStatus(STATUS.LIVE)
      setErrorDetail(null)
    } catch (e) {
      console.error('Refresh failed:', e)
      setStatus(STATUS.ERROR)
      setErrorDetail(
        API_URL_MISSING
          ? 'VITE_API_URL is not set on this deployment, so the dashboard is calling itself instead of the API.'
          : e.code === 'ECONNABORTED'
            ? 'The API did not respond in time. It may be a cold start — retrying automatically.'
            : `Could not reach the API${e.response ? ` (HTTP ${e.response.status})` : ''}. Retrying automatically.`
      )
    }
  }, [])

  // Auto-refresh every 5 seconds
  useInterval(refresh, 5000)

  const handleSeed = async () => {
    setSeeding(true)
    setSeedMsg(null)
    try {
      const res = await seedDatabase()
      setSeedMsg(`✅ Seeded: ${res.advertisers} advertisers, ${res.campaigns} campaigns, ${res.auctions_simulated} auctions`)
      await refresh()
    } catch (e) {
      // A timeout doesn't mean the seed failed — it's usually still running
      // server-side, so don't tell the visitor something untrue.
      setSeedMsg(
        e.code === 'ECONNABORTED'
          ? '⏳ Seeding is taking a while — it is still running. The dashboard will fill in on the next refresh.'
          : '❌ Seed failed — the backend could not be reached.'
      )
    } finally {
      setSeeding(false)
    }
  }

  const nav = { display: 'flex', alignItems: 'center', gap: 8 }

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)' }}>

      {/* ── Header ─────────────────────────────────────────────────────── */}
      <header className="site-header" style={{
        background: 'var(--surface)', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        position: 'sticky', top: 0, zIndex: 10,
      }}>
        <div style={{ ...nav, gap: 12 }}>
          <Activity size={20} color="var(--accent)" />
          <span style={{ fontWeight: 700, fontSize: 16 }}>RTB Auction Engine</span>
          <span style={{
            fontSize: 10,
            background: status === STATUS.LIVE ? 'rgba(34,197,94,0.15)'
              : status === STATUS.ERROR ? 'rgba(239,68,68,0.15)' : 'rgba(245,158,11,0.15)',
            color: status === STATUS.LIVE ? 'var(--green)'
              : status === STATUS.ERROR ? 'var(--red)' : 'var(--amber)',
            padding: '2px 8px', borderRadius: 20, fontWeight: 600, letterSpacing: '0.08em',
          }}>
            {status === STATUS.LIVE ? 'LIVE' : status === STATUS.ERROR ? 'OFFLINE' : 'CONNECTING'}
          </span>
        </div>
        <div style={{ ...nav, gap: 12 }}>
          {lastRefresh && (
            <span style={{ fontSize: 11, color: 'var(--muted)' }}>
              Updated {lastRefresh.toLocaleTimeString()}
            </span>
          )}
          <button onClick={refresh} style={{
            ...nav, gap: 6, background: 'var(--surface2)',
            border: '1px solid var(--border)', borderRadius: 6,
            padding: '6px 12px', color: 'var(--text)', fontSize: 12,
          }}>
            <RefreshCw size={12} /> Refresh
          </button>
          <button onClick={handleSeed} disabled={seeding} style={{
            ...nav, gap: 6,
            background: seeding ? 'var(--border)' : 'rgba(99,102,241,0.15)',
            border: '1px solid var(--accent)', borderRadius: 6,
            padding: '6px 14px', color: 'var(--accent2)', fontSize: 12, fontWeight: 600,
          }}>
            {seeding ? <><Loader size={12} /> Seeding…</> : <><Database size={12} /> Seed Demo Data</>}
          </button>
        </div>
      </header>

      {/* ── Connection state ───────────────────────────────────────────── */}
      {status !== STATUS.LIVE && (
        <div className="banner" style={{
          background: status === STATUS.ERROR ? 'rgba(239,68,68,0.1)' : 'rgba(245,158,11,0.1)',
          border: `1px solid ${status === STATUS.ERROR ? 'var(--red)' : 'var(--amber)'}`,
          color: status === STATUS.ERROR ? 'var(--red)' : 'var(--amber)',
        }}>
          {status === STATUS.CONNECTING
            ? 'Waking up the backend — the first request after an idle period can take up to a minute…'
            : errorDetail}
        </div>
      )}

      {/* ── Empty state ────────────────────────────────────────────────── */}
      {status === STATUS.LIVE && overview?.total_auctions === 0 && (
        <div className="banner" style={{
          background: 'rgba(99,102,241,0.1)', border: '1px solid var(--accent)',
          color: 'var(--accent2)',
        }}>
          No data yet — hit <strong>Seed Demo Data</strong> to create advertisers,
          campaigns and ad slots, then run auctions of your own.
        </div>
      )}

      {/* ── Seed message ───────────────────────────────────────────────── */}
      {seedMsg && (
        <div className="banner" style={{
          background: seedMsg.startsWith('✅') ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)',
          border: `1px solid ${seedMsg.startsWith('✅') ? 'var(--green)' : 'var(--red)'}`,
          color: seedMsg.startsWith('✅') ? 'var(--green)' : 'var(--red)',
        }}>
          {seedMsg}
        </div>
      )}

      <main className="page" style={{ maxWidth: 1400, margin: '0 auto' }}>

        {/* ── KPI Cards ──────────────────────────────────────────────────── */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 16, marginBottom: 24 }}>
          <StatCard
            label="Total Auctions"
            value={overview?.total_auctions?.toLocaleString() ?? '—'}
            sub="All time"
            icon={Activity}
          />
          <StatCard
            label="Fill Rate"
            value={overview ? `${overview.fill_rate}%` : '—'}
            sub="Auctions with winner"
            accent={overview?.fill_rate > 70 ? 'var(--green)' : 'var(--amber)'}
            icon={TrendingUp}
          />
          <StatCard
            label="Avg Clearing CPM"
            value={overview ? `$${(overview.avg_clearing_price_cents / 100).toFixed(3)}` : '—'}
            sub="Avg price winner pays"
            icon={TrendingUp}
          />
          <StatCard
            label="Avg Bidders/Auction"
            value={overview?.avg_bidders_per_auction?.toFixed(1) ?? '—'}
            sub="Competition level"
            icon={Users}
          />
          <StatCard
            label="Impressions"
            value={overview?.total_impressions?.toLocaleString() ?? '—'}
            sub="Ads served"
            icon={Activity}
          />
          <StatCard
            label="Overall CTR"
            value={overview ? `${overview.overall_ctr}%` : '—'}
            sub="Clicks / impressions"
            accent={overview?.overall_ctr > 1 ? 'var(--green)' : 'var(--text)'}
            icon={TrendingUp}
          />
        </div>

        {/* ── Chart + Simulator ──────────────────────────────────────────── */}
        <div className="split" style={{ display: 'grid', gap: 16, marginBottom: 16 }}>
          <AuctionChart data={timeseries} />
          <BidSimulator slots={slots} onAuctionComplete={refresh} />
        </div>

        {/* ── Auction Feed ───────────────────────────────────────────────── */}
        <div style={{ marginBottom: 16 }}>
          <AuctionFeed auctions={recentAuctions} />
        </div>

        {/* ── Campaign Table ─────────────────────────────────────────────── */}
        <CampaignTable campaigns={campaignStats} allCampaigns={allCampaigns} />

      </main>
    </div>
  )
}
