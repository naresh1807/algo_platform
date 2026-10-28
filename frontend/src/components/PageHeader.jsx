import { ArrowUpRight } from "lucide-react";
import { Link, useLocation } from "react-router-dom";
import { PAGE_DETAILS } from "../constants/navigation.js";

export default function PageHeader() {
  const { pathname } = useLocation();
  const page = PAGE_DETAILS[pathname];
  if (!page) return null;
  return <div className="page-heading">
    <div><span className="page-eyebrow">{page.section}</span><h1>{page.title}</h1><p>{page.description}</p></div>
    {pathname !== "/option-trading" && <Link to="/option-trading" className="page-trade-link">Trade options <ArrowUpRight size={16} /></Link>}
  </div>;
}
