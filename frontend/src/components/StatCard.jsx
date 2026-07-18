export default function StatCard({ label, value, sub, accent, icon: Icon }) {
  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius)',
      padding: '20px 24px',
      display: 'flex',
      flexDirection: 'column',
      gap: 6,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ color: 'var(--muted)', fontSize: 12, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
          {label}
        </span>
        {Icon && <Icon size={16} color="var(--muted)" />}
      </div>
      <div style={{
        fontSize: 28,
        fontWeight: 700,
        color: accent || 'var(--text)',
        lineHeight: 1.2,
      }}>
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>{sub}</div>
      )}
    </div>
  )
}
