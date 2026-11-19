import { Route, Routes } from 'react-router-dom'
import Layout from './components/Layout.jsx'
import AccountDetail from './pages/AccountDetail.jsx'
import Accounts from './pages/Accounts.jsx'
import Approve from './pages/Approve.jsx'
import Evaluation from './pages/Evaluation.jsx'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Accounts />} />
        <Route path="accounts/:id" element={<AccountDetail />} />
        <Route path="approve" element={<Approve />} />
        <Route path="evaluation" element={<Evaluation />} />
      </Route>
    </Routes>
  )
}
