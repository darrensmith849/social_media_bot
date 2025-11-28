import { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";

interface AppLayoutProps {
    children: ReactNode;
}

export const AppLayout = ({ children }: AppLayoutProps) => {
    return (
        <div className="layout-container">
            {/* Sidebar */}
            <aside className="sidebar">
                <div className="sidebar-header">
                    <Link to="/" className="app-logo">Postify</Link>
                </div>

                <nav className="sidebar-nav">
                    <div className="nav-section">
                        <span className="nav-label">Main</span>
                        <NavLink to="/" className={({ isActive }) => isActive ? "nav-item active" : "nav-item"}>
                            📊 Dashboard
                        </NavLink>
                        <NavLink to="/add-client" className={({ isActive }) => isActive ? "nav-item active" : "nav-item"}>
                            ➕ Add Client
                        </NavLink>
                    </div>

                    <div className="nav-section">
                        <span className="nav-label">System</span>
                        <a href="#" className="nav-item">⚙️ Settings</a>
                        <a href="#" className="nav-item">🚪 Logout</a>
                    </div>
                </nav>
            </aside>

            {/* Main Content */}
            <main className="main-content">
                <header className="top-bar">
                    <span className="breadcrumb">Agency Overview</span>
                    <div className="user-profile">
                        <div className="avatar">A</div>
                    </div>
                </header>
                <div className="page-content">
                    {children}
                </div>
            </main>

            {/* Global Styles for Layout */}
            <style>{`
        .layout-container { display: flex; height: 100vh; background: #f8fafc; }
        
        /* Sidebar */
        .sidebar { width: 260px; background: #1e293b; color: white; display: flex; flex-direction: column; flex-shrink: 0; }
        .sidebar-header { padding: 1.5rem; border-bottom: 1px solid #334155; }
        .app-logo { font-size: 1.25rem; font-weight: 700; color: white; text-decoration: none; }
        
        .sidebar-nav { padding: 1.5rem; display: flex; flex-direction: column; gap: 2rem; }
        .nav-section { display: flex; flex-direction: column; gap: 0.5rem; }
        .nav-label { text-transform: uppercase; font-size: 0.75rem; color: #94a3b8; font-weight: 600; padding-left: 0.75rem; margin-bottom: 0.25rem; }
        
        .nav-item { 
          display: block; padding: 0.75rem; border-radius: 6px; 
          color: #cbd5e1; text-decoration: none; font-size: 0.95rem; transition: all 0.2s; 
        }
        .nav-item:hover { background: #334155; color: white; }
        .nav-item.active { background: #3b82f6; color: white; }

        /* Main Area */
        .main-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
        
        .top-bar { 
          height: 64px; background: white; border-bottom: 1px solid #e2e8f0; 
          display: flex; alignItems: center; justify-content: space-between; padding: 0 2rem;
        }
        .breadcrumb { font-weight: 600; color: #475569; }
        .avatar { width: 32px; height: 32px; background: #e2e8f0; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #64748b; }

        .page-content { flex: 1; overflow-y: auto; padding: 2rem; }
      `}</style>
        </div>
    );
};
