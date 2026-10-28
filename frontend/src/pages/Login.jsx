import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowUpRight, BarChart3, Eye, EyeOff, LineChart, ShieldCheck, Wallet } from "lucide-react";
import { isWrappedApp } from "../config/backendOrigin.js";
import { ChangeServerLink } from "./ServerSettings.jsx";
import { api } from "../services/api.js";
import { storeAuthenticationToken } from "../utils/websocket.js";

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const handleSubmit = async (event) => {
    event.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api.post("/auth/token/", { username, password });
      storeAuthenticationToken(res.data.token);
      const destination = location.state?.from?.pathname;
      navigate(destination?.startsWith("/") && !destination.startsWith("//") ? destination : "/", { replace: true });
    } catch (err) {
      const status = err.response?.status;
      setError(status === 400 || status === 401
        ? "Your username or password is incorrect."
        : !status || status >= 500
          ? "The sign-in server is unavailable. Please try again shortly."
          : status === 429
            ? "Too many sign-in attempts. Please wait and try again."
            : "Sign-in could not be completed. Please try again.");
    } finally { setLoading(false); }
  };
  return <div className="login-page">
    <aside className="login-intro">
      <div className="brand"><span className="brand-mark"><LineChart size={24} /></span><span className="brand-text">Algo<span>Trade</span></span></div>
      <div className="login-intro-copy"><span className="page-eyebrow">Your trading workspace</span><h1>A clearer view<br />of your next trade.</h1><p>Charts, options, and your portfolio.<br />Together in one focused workspace.</p>
        <div className="login-feature-list"><span><BarChart3 size={20} />Market charts & option analytics</span><span><Wallet size={20} />Orders & portfolio tracking</span><span><ShieldCheck size={20} />Risk controls at every step</span></div>
      </div>
      <span className="login-intro-footer">Built for thoughtful trading.</span>
    </aside>
    <main className="login-form-area">
      <form className="login-form" onSubmit={handleSubmit}>
        <span className="page-eyebrow">Welcome back</span><h2>Sign in to your workspace</h2><p>Continue with your platform account.</p>
        <label htmlFor="login-username">Username</label>
        <input className="input" id="login-username" name="username" autoComplete="username" required autoFocus value={username} onChange={(event) => setUsername(event.target.value)} placeholder="Enter your username" disabled={loading} />
        <label htmlFor="login-password">Password</label>
        <div className="password-input"><input className="input" id="login-password" name="password" autoComplete="current-password" type={visible ? "text" : "password"} required value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Enter your password" disabled={loading} />
          <button type="button" aria-label={visible ? "Hide password" : "Show password"} onClick={() => setVisible((value) => !value)}>{visible ? <EyeOff size={18} /> : <Eye size={18} />}</button>
        </div>
        {error && <p role="alert" className="login-error">{error}</p>}
        <button type="submit" className="btn btn-primary login-submit" disabled={loading}>{loading ? "Signing in…" : "Sign in"}<ArrowUpRight size={18} /></button>
        <div className="login-account-note"><ShieldCheck size={16} /><span>Use your existing platform account to continue.</span></div>
        {isWrappedApp() && <ChangeServerLink />}
      </form>
    </main>
  </div>;
}
