import axios from 'axios'

export const API_BASE = import.meta.env.VITE_API_URL || '/api/v1'

// In production the fallback is a dead end: '/api/v1' resolves against the
// static host, which serves the SPA, not the API. Surface that loudly instead
// of leaving the dashboard silently empty.
export const API_URL_MISSING =
  import.meta.env.PROD && !import.meta.env.VITE_API_URL

const api = axios.create({
  baseURL: API_BASE,
  // Generous: a cold serverless function or a sleeping free-tier database can
  // take the better part of a minute to answer the first request.
  timeout: 45000,
})

// ── Analytics ────────────────────────────────────────────────────────────────
export const fetchOverview    = () => api.get('/analytics/overview').then(r => r.data)
export const fetchCampaignStats = () => api.get('/analytics/campaigns').then(r => r.data)
export const fetchTimeseries  = () => api.get('/analytics/timeseries').then(r => r.data)

// ── Auction ──────────────────────────────────────────────────────────────────
export const fetchRecentAuctions = (limit = 20) =>
  api.get(`/auction/recent?limit=${limit}`).then(r => r.data)

export const submitBidRequest = (payload) =>
  api.post('/auction/bid', payload).then(r => r.data)

// ── Entities ─────────────────────────────────────────────────────────────────
export const fetchAdvertisers = () => api.get('/advertisers/').then(r => r.data)
export const fetchCampaigns   = () => api.get('/campaigns/').then(r => r.data)
export const fetchPublishers  = () => api.get('/publishers/').then(r => r.data)
export const fetchAdSlots     = () => api.get('/publishers/slots').then(r => r.data)

// ── Seed ─────────────────────────────────────────────────────────────────────
// Seeding wipes and rebuilds the dataset then simulates 60 auctions — it is
// the slowest call in the app, so it gets the full serverless budget.
export const seedDatabase = () =>
  api.post('/seed/', null, { timeout: 60000 }).then(r => r.data)

export const checkHealth = () =>
  api.get('/health', { baseURL: API_BASE.replace(/\/api\/v1\/?$/, ''), timeout: 60000 })
    .then(r => r.data)

export default api
