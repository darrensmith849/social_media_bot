import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AppLayout } from "./components/AppLayout";
import { DashboardPage } from "./pages/DashboardPage";
import { ClientOverviewPage } from "./pages/ClientOverviewPage";
import { ClientApprovalsPage } from "./pages/ClientApprovalsPage";
import { ClientSettingsPage } from "./pages/ClientSettingsPage";
import { AddClientPage } from "./pages/AddClientPage";

function App() {
  return (
    <BrowserRouter>
      <AppLayout>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/add-client" element={<AddClientPage />} />
          <Route path="/clients/:clientId" element={<ClientOverviewPage />} />
          <Route path="/clients/:clientId/approvals" element={<ClientApprovalsPage />} />
          <Route path="/clients/:clientId/settings" element={<ClientSettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AppLayout>
    </BrowserRouter>
  );
}

export default App;
