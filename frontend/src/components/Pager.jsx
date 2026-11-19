export default function Pager({ page, total, pageSize, onPage }) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const from = total ? (page - 1) * pageSize + 1 : 0
  const to = Math.min(page * pageSize, total)
  const start = Math.max(1, Math.min(page - 2, pages - 4))
  const nums = []
  for (let n = start; n <= Math.min(pages, start + 4); n++) nums.push(n)

  return (
    <div className="pager">
      <span>Showing {from} - {to} of {total}</span>
      <button className="page-btn" disabled={page === 1} onClick={() => onPage(1)} aria-label="First page">«</button>
      <button className="page-btn" disabled={page === 1} onClick={() => onPage(page - 1)} aria-label="Previous page">‹</button>
      {nums.map((n) => (
        <button key={n} className={'page-btn' + (n === page ? ' active' : '')} onClick={() => onPage(n)}>{n}</button>
      ))}
      {start + 4 < pages && (
        <>
          <span>…</span>
          <button className="page-btn" onClick={() => onPage(pages)}>{pages}</button>
        </>
      )}
      <button className="page-btn" disabled={page === pages} onClick={() => onPage(page + 1)} aria-label="Next page">›</button>
      <button className="page-btn" disabled={page === pages} onClick={() => onPage(pages)} aria-label="Last page">»</button>
    </div>
  )
}
