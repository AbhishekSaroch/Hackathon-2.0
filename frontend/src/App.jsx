import { Route, Routes } from 'react-router-dom'
import Layout from './components/Layout.jsx'
import AccountDetail from './pages/AccountDetail.jsx'
import Accounts from './pages/Accounts.jsx'
import Approve from './pages/Approve.jsx'
import EarlyWarning from './pages/EarlyWarning.jsx'
import Evaluation from './pages/Evaluation.jsx'
import FlagDetail from './pages/FlagDetail.jsx'
import Nudges from './pages/Nudges.jsx'
import RiskEvaluation from './pages/RiskEvaluation.jsx'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        {/* BI-02: why late customers are not paying */}
        <Route index element={<Accounts />} />
        <Route path="accounts/:id" element={<AccountDetail />} />
        <Route path="approve" element={<Approve />} />
        <Route path="evaluation" element={<Evaluation />} />
        {/* BI-03: who may miss the next EMI */}
        <Route path="early-warning" element={<EarlyWarning />} />
        <Route path="early-warning/:id" element={<FlagDetail />} />
        <Route path="nudges" element={<Nudges />} />
        <Route path="risk-evaluation" element={<RiskEvaluation />} />
      </Route>
    </Routes>
  )
}
