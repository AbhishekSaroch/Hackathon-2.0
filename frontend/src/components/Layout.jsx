import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api, getAskUser, getLcUser, setAskUser, setLcUser } from '../api.js'

// Simple line icons (no icon library needed)
const Icon = {
  accounts: <svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10" /></svg>,
  approve: <svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2" /><path d="M8 12.5l3 3 5-6" /></svg>,
  evaluation: <svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M2 20h20" /></svg>,
  warning: <svg viewBox="0 0 24 24"><path d="M12 3l9.5 17h-19L12 3z" /><path d="M12 10v5M12 17.5v.5" /></svg>,
  nudge: <svg viewBox="0 0 24 24"><path d="M21 4L3 11l7 3 3 7 8-17z" /><path d="M10 14l4-4" /></svg>,
  risk: <svg viewBox="0 0 24 24"><path d="M3 17l5-6 4 3 8-9" /><path d="M15 5h5v5" /></svg>,
  command: <svg viewBox="0 0 24 24"><rect x="3" y="4" width="8" height="7" rx="1" /><rect x="13" y="4" width="8" height="4" rx="1" /><rect x="3" y="13" width="8" height="4" rx="1" /><rect x="13" y="10" width="8" height="10" rx="1" /></svg>,
  incident: <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" /><path d="M12 7v6M12 16v.5" /></svg>,
  catalog: <svg viewBox="0 0 24 24"><path d="M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3V4z" /><path d="M9 9h6M9 13h6" /></svg>,
  scorecard: <svg viewBox="0 0 24 24"><path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9L12 3z" /></svg>,
  ask: <svg viewBox="0 0 24 24"><path d="M4 5h16v11H9l-5 4V5z" /><path d="M9.5 9.5a2.5 2.5 0 114 2c-.9.6-1.5 1-1.5 2M12 15.5v.5" /></svg>,
  defs: <svg viewBox="0 0 24 24"><path d="M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3V4z" /><path d="M9 9h6M9 13h4" /></svg>,
  askeval: <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" /><path d="M8.5 12.5l2.5 2.5 4.5-5" /></svg>,
}

// The sidebar: one block per module (its name, then its pages). `hidden: true` keeps a page and its route but leaves it out of the menu,
// so it can be shown again by deleting that one flag.
const MODULES = [
  { code: 'BI-02', name: 'Reason finder', items: [
    { to: '/', label: 'Accounts', icon: Icon.accounts, end: true },
    { to: '/approve', label: 'To approve', icon: Icon.approve },
    { to: '/evaluation', label: 'Evaluation', icon: Icon.evaluation, hidden: true },
  ] },
  { code: 'BI-03', name: 'Early warning', items: [
    { to: '/early-warning', label: 'Flagged accounts', icon: Icon.warning },
    { to: '/nudges', label: 'Nudges', icon: Icon.nudge },
    { to: '/risk-evaluation', label: 'Risk evaluation', icon: Icon.risk, hidden: true },
  ] },
  { code: 'LC-04', name: 'Command center', items: [
    { to: '/command', label: 'Live view', icon: Icon.command, end: true },
    { to: '/command/incidents', label: 'Incidents', icon: Icon.incident },
    { to: '/command/metrics', label: 'Metric catalog', icon: Icon.catalog, hidden: true },
    { to: '/command/scorecard', label: 'Scorecard', icon: Icon.scorecard, hidden: true },
  ] },
  { code: 'LC-01', name: 'Ask your portfolio', items: [
    { to: '/ask', label: 'Ask a question', icon: Icon.ask, end: true },
    { to: '/ask/definitions', label: 'Metric definitions', icon: Icon.defs, hidden: true },
    { to: '/ask/evaluation', label: 'Ask evaluation', icon: Icon.askeval, hidden: true },
  ] },
]
const BI03_PATHS = ['/early-warning', '/nudges', '/risk-evaluation']
const LC04_PATH = '/command'
const LC01_PATH = '/ask'

export default function Layout() {
  const [pendingCount, setPendingCount] = useState(0)
  const [nudgeCount, setNudgeCount] = useState(0)
  const { pathname } = useLocation()
  const inBi03 = BI03_PATHS.some((p) => pathname.startsWith(p))
  const inLc04 = pathname.startsWith(LC04_PATH)
  const [lcUsers, setLcUsers] = useState([])
  const [lcUser, setLcUserState] = useState(getLcUser())
  useEffect(() => { if (inLc04 && !lcUsers.length) api('/lc04/users').then(setLcUsers).catch(() => {}) }, [inLc04])
  const inLc01 = pathname.startsWith(LC01_PATH)
  const [askUsers, setAskUsers] = useState([])
  const [askUser, setAskUserState] = useState(getAskUser())
  useEffect(() => {
    if (inLc01 && !askUsers.length) {
      api('/lc01/users').then((us) => {
        setAskUsers(us)
        if (us.length && !us.some((u) => u.user_id === getAskUser())) { setAskUser(us[0].user_id); setAskUserState(us[0].user_id) }   // a user remembered from before is replaced by the one the app now offers
      }).catch(() => {})
    }
  }, [inLc01])

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
          {MODULES.map((m) => (
            <div key={m.code} className="nav-module">
              <div className="nav-module-head"><span className="nav-name">{m.name}</span></div>
              {m.items.filter((n) => !n.hidden).map((n) => (
                <NavLink key={n.to} to={n.to} end={n.end} title={`${m.name}: ${n.label}`} aria-label={n.label}
                  className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}>
                  {n.icon}
                  <span className="nav-label">{n.label}</span>
                  {n.to === '/approve' && pendingCount > 0 && <span className="nav-badge">{pendingCount}</span>}
                  {n.to === '/nudges' && nudgeCount > 0 && <span className="nav-badge">{nudgeCount > 99 ? '99+' : nudgeCount}</span>}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>

      <header className="topbar">
        <div className="logo">Portfolio<b>IQ</b><small>{inLc01 ? 'Ask your portfolio' : inLc04 ? 'Operations command center' : inBi03 ? 'Who may miss the next EMI' : 'Why customers are not paying'}</small></div>
        <span className="spacer" />
        {inLc01 ? (
          <label className="who">Signed in as{' '}
            <select className="select" value={askUser} onChange={(e) => { setAskUser(e.target.value); setAskUserState(e.target.value) }}>
              {askUsers.map((u) => <option key={u.user_id} value={u.user_id}>{u.name}</option>)}
            </select>
          </label>
        ) : inLc04 ? (
          <label className="who">Viewing as{' '}
            <select className="select" value={lcUser} onChange={(e) => { setLcUser(e.target.value); setLcUserState(e.target.value) }}>
              {lcUsers.map((u) => <option key={u.user_id} value={u.user_id}>{u.name}</option>)}
            </select>
          </label>
        ) : <span className="who">Neha Kapoor · Collections Head</span>}
        <div className="avatar">{inLc01 ? (askUsers.find((u) => u.user_id === askUser)?.name || 'A')[0] : inLc04 ? (lcUsers.find((u) => u.user_id === lcUser)?.name || 'A')[0] : 'N'}</div>
      </header>

      <main className="content"><Outlet /></main>

      <footer className="footer">
        <div><span>Feedback</span><span>Help</span></div>
        <div><span>© 2026 PortfolioIQ · prototype, all data is synthetic</span><span>Privacy</span><span>Terms</span></div>
      </footer>
    </div>
  )
}
