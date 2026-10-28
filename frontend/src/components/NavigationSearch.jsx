import { useEffect, useState } from "react";
import { ArrowUpRight, Search, X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import AppDialog from "./AppDialog.jsx";
import { NAV_SECTIONS, SETTINGS_ITEM } from "../constants/navigation.js";

const destinations = [...NAV_SECTIONS.flatMap((section) => section.items.map((item) => ({ ...item, section: section.label }))), { ...SETTINGS_ITEM, section: "Account" }];

export default function NavigationSearch() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const navigate = useNavigate();
  useEffect(() => {
    const handleKey = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, []);
  const results = destinations.filter((item) => `${item.label} ${item.section}`.toLowerCase().includes(query.toLowerCase()));
  const go = (to) => { setOpen(false); setQuery(""); navigate(to); };
  const askDrishti = () => {
    const question = query.trim();
    setOpen(false); setQuery("");
    window.dispatchEvent(new CustomEvent("jarvis:open", { detail: { question } }));
  };
  return <>
    <button type="button" className="navigation-search-trigger" onClick={() => setOpen(true)} aria-label="Search pages and trading tools">
      <Search size={17} /><span>Ask Drishti / Search tools</span><kbd>Ctrl K</kbd>
    </button>
    <AppDialog open={open} onClose={() => setOpen(false)} className="navigation-search-dialog" label="Search pages and trading tools">
      <div className="navigation-search-input">
        <Search size={20} />
        <input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find orders, options, portfolio…" aria-label="Search pages"
          onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); if (results[0]) go(results[0].to); else if (query.trim()) askDrishti(); } }} />
        <button className="icon-btn" onClick={() => setOpen(false)} aria-label="Close search"><X size={18} /></button>
      </div>
      <div className="navigation-search-results">
        <button type="button" className="navigation-search-result" onClick={askDrishti}><span className="navigation-result-icon"><Search size={19} /></span><span><strong>Ask Drishti{query.trim() ? `: ${query.trim()}` : " about the market"}</strong><small>Market summary, outlook, news and index details</small></span><ArrowUpRight size={16} /></button>
        {results.length ? results.map(({ to, label, icon: Icon, section }) => (
          <button className="navigation-search-result" key={to} onClick={() => go(to)}>
            <span className="navigation-result-icon"><Icon size={19} /></span>
            <span><strong>{label}</strong><small>{section}</small></span><ArrowUpRight size={16} />
          </button>
        )) : <p className="navigation-no-results">No matching pages. Try “orders” or “options”.</p>}
      </div>
      <div className="navigation-search-footer">Enter to open the first result · Esc to close</div>
    </AppDialog>
  </>;
}
