// LC-04 (command center) calls are made AS a demo user: the backend reads that user's role and lender scope from its own records.
export const getLcUser = () => { try { return localStorage.getItem('lc04_user') || 'u_leader' } catch { return 'u_leader' } }
export const setLcUser = (u) => { try { localStorage.setItem('lc04_user', u) } catch { /* private mode */ } window.dispatchEvent(new Event('lc04-user-changed')) }
export const notifyLcClockChanged = () => window.dispatchEvent(new Event('lc04-clock-changed'))

// LC-01 (ask your portfolio) calls are made AS a demo lender user too; the backend reads the lender scope from that user's record.
export const getAskUser = () => { try { return localStorage.getItem('lc01_user') || 'u_l1' } catch { return 'u_l1' } }
export const setAskUser = (u) => { try { localStorage.setItem('lc01_user', u) } catch { /* private mode */ } window.dispatchEvent(new Event('lc01-user-changed')) }

// All backend calls go through here. Vite proxies /bi02, /bi03, /lc04 and /lc01 to FastAPI (port 8000).
export async function api(path, { method = 'GET', body } = {}) {
  let res
  const headers = { 'Content-Type': 'application/json' }
  if (path.startsWith('/lc04')) headers['X-User-Id'] = getLcUser()
  if (path.startsWith('/lc01')) headers['X-User-Id'] = getAskUser()
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new Error('Cannot reach the backend. Start FastAPI on port 8000.')
  }
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`)
  return data
}

// Tell the sidebar badge to refresh (after approve / skip / new assessments)
export const notifyRecsChanged = () => window.dispatchEvent(new Event('recs-changed'))
// Same for the BI-03 nudge queue badge (after approve / skip / replay changes)
export const notifyNudgesChanged = () => window.dispatchEvent(new Event('nudges-changed'))
