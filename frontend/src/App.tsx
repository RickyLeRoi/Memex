import { useEffect, useState } from "react";
import { applyThemeMode, nextThemeMode, readThemeMode, type ThemeMode } from "./theme";
import { Areas } from "./views/Areas";
import { Dashboard } from "./views/Dashboard";
import { Links } from "./views/Links";
import { SourceFamily } from "./views/SourceFamily";

const TABS = [
  { id: "dashboard", label: "Dashboard", icon: "M3 3h8v8H3zM13 3h8v5h-8zM13 10h8v11h-8zM3 13h8v8H3z" },
  { id: "links", label: "Link", icon: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" },
  { id: "areas", label: "Aree", icon: "M12 2 2 7l10 5 10-5zM2 12l10 5 10-5M2 17l10 5 10-5" },
  { id: "chat", label: "Chat", icon: "M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-5.4A8 8 0 1 1 21 12z" },
  { id: "mail", label: "Email", icon: "M3 5h18v14H3zM3 6l9 7 9-7" },
  { id: "tickets", label: "Ticket", icon: "M3 8a2 2 0 0 0 0 4v0a2 2 0 0 1 0 4v2h18v-2a2 2 0 0 1 0-4 2 2 0 0 0 0-4V6H3zM14 6v12" },
] as const;

const THEME_LABEL: Record<ThemeMode, string> = { auto: "Auto", light: "Chiaro", dark: "Scuro" };
const THEME_ICON: Record<ThemeMode, string> = {
  auto: "M12 3a9 9 0 1 0 0 18zM12 3v18",
  light: "M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM12 1v3M12 20v3M1 12h3M20 12h3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
  dark: "M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z",
};

type TabId = (typeof TABS)[number]["id"];

function initialTab(): TabId {
  const hash = window.location.hash.slice(1);
  return TABS.some((t) => t.id === hash) ? (hash as TabId) : "dashboard";
}

function Icon({ path }: { path: string }) {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={path} />
    </svg>
  );
}

export function App() {
  const [tab, setTab] = useState<TabId>(initialTab);
  const [themeMode, setThemeMode] = useState<ThemeMode>(readThemeMode);
  const select = (id: TabId) => {
    window.location.hash = id;
  };

  useEffect(() => {
    const sync = () => setTab(initialTab());
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  const cycleTheme = () => {
    const next = nextThemeMode(themeMode);
    applyThemeMode(next);
    setThemeMode(next);
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>Memex</h1>
        <nav className="nav" aria-label="Sezioni">
          {TABS.map((t) => (
            <button key={t.id} className={`tab ${tab === t.id ? "active" : ""}`} aria-current={tab === t.id ? "page" : undefined} onClick={() => select(t.id)}>
              <Icon path={t.icon} />
              <span>{t.label}</span>
            </button>
          ))}
        </nav>
        <button className="theme-toggle" onClick={cycleTheme} title="Cambia tema" aria-label={`Tema: ${THEME_LABEL[themeMode]}. Clicca per cambiare`}>
          <Icon path={THEME_ICON[themeMode]} />
          <span>{THEME_LABEL[themeMode]}</span>
        </button>
      </header>
      <main className={tab === "dashboard" ? "page page-dashboard" : "page"}>
        {tab === "dashboard" && <Dashboard />}
        {tab === "links" && <Links />}
        {tab === "areas" && <Areas />}
        {(tab === "chat" || tab === "mail" || tab === "tickets") && <SourceFamily key={tab} family={tab} />}
      </main>
    </div>
  );
}
