import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api/v1',
  timeout: 15000,
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
export const seedDatabase = () => api.post('/seed/').then(r => r.data)

export default api
