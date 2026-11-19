import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api } from '../api.js'

// Simple line icons (no icon library needed)
const Icon = {
  accounts: <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10" /></svg>,
  approve: <svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2" /><path d="M8 12.5l3 3 5-6" /></svg>,
  evaluation: <svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M2 20h20" /></svg>,
}

const NAV = [
  { to: '/', label: 'Accounts', icon: Icon.accounts, end: true },
  { to: '/approve', label: 'To approve', icon: Icon.approve },
  { to: '/evaluation', label: 'Evaluation', icon: Icon.evaluation },
]

export default function Layout() {
  const [pendingCount, setPendingCount] = useState(0)
  const { pathname } = useLocation()

  // sidebar badge = recommendations waiting for approval
  useEffect(() => {
    const refresh = () => api('/bi02/recommendations?status=pending').then((r) => setPendingCount(r.length)).catch(() => {})
    refresh()
    window.addEventListener('recs-changed', refresh)
    return () => window.removeEventListener('recs-changed', refresh)
  }, [pathname])

  return (
    <div className="app">
      <aside className="sidebar">
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} title={n.label} aria-label={n.label}
              className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}>
              {n.icon}
              {n.to === '/approve' && pendingCount > 0 && <span className="nav-badge">{pendingCount}</span>}
            </NavLink>
          ))}
        </nav>
      </aside>

      <header className="topbar">
        <div className="logo">Portfolio<b>IQ</b><small>Why customers are not paying</small></div>
        <span className="spacer" />
        <span className="who">Neha Kapoor · Collections Head</span>
        <div className="avatar">N</div>
      </header>

      <main className="content"><Outlet /></main>

      <footer className="footer">
        <div><span>Feedback</span><span>Help</span></div>
        <div><span>© 2026 PortfolioIQ · prototype, all data is synthetic</span><span>Privacy</span><span>Terms</span></div>
      </footer>
    </div>
  )
}
