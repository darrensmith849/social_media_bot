import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";

interface AppLayoutProps {
    children: ReactNode;
}

export const AppLayout = ({ children }: AppLayoutProps) => {
    return (
        <div className="app-root">
            <header className="app-header">
                <div className="app-header-left">
                    <Link to="/" className="app-logo">
                        Postify
                    </Link>
                    <nav className="app-nav">
                        <NavLink to="/" className={({ isActive }) =>
                            isActive ? "app-nav-link active" : "app-nav-link"
                        }>
                            Dashboard
                        </NavLink>
                    </nav>
                </div>
                <div className="app-header-right">
                    {/* Later: user avatar / logout / plan badge */}
                    <span className="app-badge">Beta</span>
                </div>
            </header>
            <main className="app-main">{children}</main>
        </div>
    );
};
