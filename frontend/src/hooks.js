import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'

// const { data, error, loading, reload } = useFetch('/bi02/metrics')
export function useFetch(path) {
  const [state, setState] = useState({ data: null, error: '', loading: true })
  const load = useCallback(async () => {
    try {
      const data = await api(path)
      setState({ data, error: '', loading: false })
    } catch (e) {
      setState({ data: null, error: e.message, loading: false })
    }
  }, [path])
  useEffect(() => { setState((s) => ({ ...s, loading: true })); load() }, [load])
  return { ...state, reload: load }
}
