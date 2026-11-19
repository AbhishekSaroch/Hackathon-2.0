// All backend calls go through here. Vite proxies /bi02 to FastAPI (port 8000).
export async function api(path, { method = 'GET', body } = {}) {
  let res
  try {
    res = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
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
