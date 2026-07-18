import { useState, useEffect, useCallback } from 'react'
import { Activity, Database, Users, TrendingUp, RefreshCw, Loader } from 'lucide-react'
import StatCard from './components/StatCard'
import AuctionChart from './components/AuctionChart'
import AuctionFeed from './components/AuctionFeed'
import CampaignTable from './components/CampaignTable'
import BidSimulator from './components/BidSimulator'
import {
  fetchOverview, fetchCampaignStats, fetchTimeseries,
  fetchRecentAuctions, fetchAdSlots, fetchCampaigns, seedDatabase
} from './api'

function useInterval(fn, ms) {
  useEffect(() => {
    fn()
    const id = setInterval(fn, ms)
    return () => clearInterval(id)
  }, [])
}

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
    } catch (e) {
      console.error('Refresh failed:', e)
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
      setSeedMsg('❌ Seed failed — is the backend running?')
    } finally {
      setSeeding(false)
    }
  }

  const nav = { display: 'flex', alignItems: 'center', gap: 8 }

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)' }}>

      {/* ── Header ─────────────────────────────────────────────────────── */}
      <header style={{
        background: 'var(--surface)', borderBottom: '1px solid var(--border)',
        padding: '0 32px', height: 56,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        position: 'sticky', top: 0, zIndex: 10,
      }}>
        <div style={{ ...nav, gap: 12 }}>
          <Activity size={20} color="var(--accent)" />
          <span style={{ fontWeight: 700, fontSize: 16 }}>RTB Auction Engine</span>
          <span style={{
            fontSize: 10, background: 'rgba(99,102,241,0.15)', color: 'var(--accent2)',
            padding: '2px 8px', borderRadius: 20, fontWeight: 600, letterSpacing: '0.08em',
          }}>LIVE</span>
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

      {/* ── Seed message ───────────────────────────────────────────────── */}
      {seedMsg && (
        <div style={{
          margin: '16px 32px 0', padding: '10px 16px',
          background: seedMsg.startsWith('✅') ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)',
          border: `1px solid ${seedMsg.startsWith('✅') ? 'var(--green)' : 'var(--red)'}`,
          borderRadius: 8, fontSize: 13,
          color: seedMsg.startsWith('✅') ? 'var(--green)' : 'var(--red)',
        }}>
          {seedMsg}
        </div>
      )}

      <main style={{ padding: '24px 32px', maxWidth: 1400, margin: '0 auto' }}>

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
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: 16, marginBottom: 16 }}>
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
