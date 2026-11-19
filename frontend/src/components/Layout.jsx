import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api } from '../api.js'

// Simple line icons (no icon library needed)
const Icon = {
  accounts: <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10" /></svg>,
  approve: <svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2" /><path d="M8 12.5l3 3 5-6" /></svg>,
  evaluation: <svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M2 20h20" /></svg>,
  warning: <svg viewBox="0 0 24 24"><path d="M12 3l9.5 17h-19L12 3z" /><path d="M12 10v5M12 17.5v.5" /></svg>,
  nudge: <svg viewBox="0 0 24 24"><path d="M21 4L3 11l7 3 3 7 8-17z" /><path d="M10 14l4-4" /></svg>,
  risk: <svg viewBox="0 0 24 24"><path d="M3 17l5-6 4 3 8-9" /><path d="M15 5h5v5" /></svg>,
}

// BI-02 group (why late customers are not paying), then BI-03 group (who may miss the next EMI)
const NAV = [
  { to: '/', label: 'Accounts', icon: Icon.accounts, end: true },
  { to: '/approve', label: 'To approve', icon: Icon.approve },
  { to: '/evaluation', label: 'Evaluation', icon: Icon.evaluation },
  { sep: true },
  { to: '/early-warning', label: 'Early warning', icon: Icon.warning },
  { to: '/nudges', label: 'Nudges', icon: Icon.nudge },
  { to: '/risk-evaluation', label: 'Risk evaluation', icon: Icon.risk },
]
const BI03_PATHS = ['/early-warning', '/nudges', '/risk-evaluation']

export default function Layout() {
  const [pendingCount, setPendingCount] = useState(0)
  const [nudgeCount, setNudgeCount] = useState(0)
  const { pathname } = useLocation()
  const inBi03 = BI03_PATHS.some((p) => pathname.startsWith(p))

  // sidebar badge = recommendations waiting for approval
  useEffect(() => {
    const refresh = () => api('/bi02/recommendations?status=pending').then((r) => setPendingCount(r.length)).catch(() => {})
    refresh()
    window.addEventListener('recs-changed', refresh)
    return () => window.removeEventListener('recs-changed', refresh)
  }, [pathname])

  // second badge = BI-03 nudges waiting for approval (silent if BI-03 has no data)
  useEffect(() => {
    const refresh = () => api('/bi03/nudges?status=pending_approval&page_size=1').then((r) => setNudgeCount(r.total)).catch(() => setNudgeCount(0))
    refresh()
    window.addEventListener('nudges-changed', refresh)
    return () => window.removeEventListener('nudges-changed', refresh)
  }, [pathname])

  return (
    <div className="app">
      <aside className="sidebar">
        <nav>
          {NAV.map((n, i) => n.sep ? <span key={i} className="nav-sep" /> : (
            <NavLink key={n.to} to={n.to} end={n.end} title={n.label} aria-label={n.label}
              className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}>
              {n.icon}
              {n.to === '/approve' && pendingCount > 0 && <span className="nav-badge">{pendingCount}</span>}
              {n.to === '/nudges' && nudgeCount > 0 && <span className="nav-badge">{nudgeCount > 99 ? '99+' : nudgeCount}</span>}
            </NavLink>
          ))}
        </nav>
      </aside>

      <header className="topbar">
        <div className="logo">Portfolio<b>IQ</b><small>{inBi03 ? 'Who may miss the next EMI' : 'Why customers are not paying'}</small></div>
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
