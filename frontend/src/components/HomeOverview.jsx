import { ArrowUpRight, ArrowLeftRight, BarChart3, ClipboardList, ShieldCheck } from "lucide-react";
import { Link } from "react-router-dom";

const actions = [
  { to: "/option-trading", title: "Trade options", detail: "Calls & puts", icon: ArrowLeftRight },
  { to: "/options", title: "Option chain", detail: "Strikes & Greeks", icon: BarChart3 },
  { to: "/orders", title: "My orders", detail: "Track your requests", icon: ClipboardList },
  { to: "/risk", title: "Risk overview", detail: "Limits & safeguards", icon: ShieldCheck },
];
export default function HomeOverview() {
  return <section className="home-overview" aria-label="Trading shortcuts">
    <div className="home-intro"><span className="page-eyebrow">Built for your trading day</span><h2>One place.<br /><span>Every market move.</span></h2>
      <p>Explore the market, review your setup,<br className="desktop-only" /> and stay in control of your trades.</p>
      <Link to="/jarvis-signals" className="home-workspace-link">Open trading workspace <ArrowUpRight size={17} /></Link>
    </div>
    <div className="home-action-grid">{actions.map(({ to, title, detail, icon: Icon }) => (
      <Link key={to} to={to} className="home-action"><span className="home-action-icon"><Icon size={23} /></span>
        <span><strong>{title}</strong><small>{detail}</small></span><ArrowUpRight size={16} />
      </Link>
    ))}</div>
  </section>;
}
