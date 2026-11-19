// Page title + grey subtitle + blue "i" dot, like the Accounts / Legal Dashboard pages
export default function PageHeader({ title, subtitle, info }) {
  return (
    <div className="page-head">
      <h1>
        {title}
        {info && <span className="info-dot" title={info}>i</span>}
      </h1>
      {subtitle && <p>{subtitle}</p>}
    </div>
  )
}
