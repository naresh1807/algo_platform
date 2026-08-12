import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { endpoints } from "../services/api.js";
import { useJarvisStore } from "../store/jarvisStore.js";

/**
 * manual 14.19 Dashboard Integration: Listening Status, Conversation
 * History, Suggested Commands, Recent Alerts, System Status -- all in
 * one floating panel so it's reachable from every page (mounted once
 * in App.jsx's AppShell, not per-page).
 *
 * Voice input uses the browser's built-in SpeechRecognition API
 * (webkitSpeechRecognition in Chrome/Edge) -- no external STT
 * API/key, matching manual 14.6's "Voice" input mode without adding a
 * paid dependency. Honest degrade: if the browser doesn't support it
 * (Firefox, most mobile Safari as of this writing), the mic button is
 * hidden and text input still works -- this is a real browser
 * capability gap, not something worth faking with a fallback that
 * silently does nothing.
 */
const SPEECH_RECOGNITION = window.SpeechRecognition || window.webkitSpeechRecognition;

// Fallback only -- used if GET /jarvis/suggested/ hasn't returned yet
// (or fails) so the empty-conversation state never shows literally
// nothing to try. apps.jarvis.commands.suggested_commands is the real
// source of truth once it loads (see the suggested-commands effect
// below); this hardcoded list existed permanently before, now it's
// just the first paint.
const FALLBACK_SUGGESTED = [
  "show risk", "open portfolio", "today's profit", "AI status",
  "kill switch status", "current strategy",
];

export default function JarvisPanel() {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [suggested, setSuggested] = useState(FALLBACK_SUGGESTED);
  const messages = useJarvisStore((s) => s.messages);
  const announcements = useJarvisStore((s) => s.announcements);
  const toasts = useJarvisStore((s) => s.toasts);
  const unseenCount = useJarvisStore((s) => s.unseenCount);
  const sending = useJarvisStore((s) => s.sending);
  const listening = useJarvisStore((s) => s.listening);
  const connected = useJarvisStore((s) => s.connected);
  const pendingConfirmText = useJarvisStore((s) => s.pendingConfirmText);
  const sendCommand = useJarvisStore((s) => s.sendCommand);
  const setListening = useJarvisStore((s) => s.setListening);
  const connectAnnouncements = useJarvisStore((s) => s.connectAnnouncements);
  const markAnnouncementsSeen = useJarvisStore((s) => s.markAnnouncementsSeen);
  const dismissToast = useJarvisStore((s) => s.dismissToast);
  const hydrateHistory = useJarvisStore((s) => s.hydrateHistory);
  const navigate = useNavigate();
  const scrollRef = useRef(null);
  const recognitionRef = useRef(null);

  useEffect(() => {
    // Connects immediately on mount, regardless of whether the panel is
    // open -- JARVIS's announcements (manual 14.16) are proactive, not a
    // response to the user opening the panel or typing a command.
    connectAnnouncements();
    // Past conversation from the backend's own log (apps.jarvis.
    // JarvisCommandHistory) -- previously `messages` reset to empty on
    // every page load even though the backend had a full record.
    hydrateHistory();
    // Real suggested commands (apps.jarvis.commands.suggested_commands)
    // instead of the permanently-hardcoded list this used to show.
    endpoints.jarvisSuggested().then((res) => {
      if (res.data.commands?.length > 0) setSuggested(res.data.commands);
    }).catch(() => {}); // FALLBACK_SUGGESTED already covers this
  }, [connectAnnouncements, hydrateHistory]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const submit = (raw, inputMode = "text") => {
    const value = (raw ?? text).trim();
    if (!value) return;
    const confirm = pendingConfirmText !== null;
    sendCommand(value, { inputMode, confirm, navigate });
    setText("");
  };

  const toggleMic = () => {
    if (!SPEECH_RECOGNITION) return;
    if (listening) {
      recognitionRef.current?.stop();
      return;
    }
    const recognition = new SPEECH_RECOGNITION();
    recognition.lang = "en-IN";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.onstart = () => setListening(true);
    recognition.onend = () => setListening(false);
    recognition.onerror = () => setListening(false);
    recognition.onresult = (event) => {
      const transcript = event.results[0][0].transcript;
      submit(transcript, "voice");
    };
    recognitionRef.current = recognition;
    recognition.start();
  };

  // Toasts render regardless of panel open/closed state -- this is what
  // lets JARVIS "tell you directly" (manual 14.16) without you having
  // to open the panel or issue any command first.
  const toastStack = (
    <div className="jarvis-toast-stack">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`jarvis-toast jarvis-toast-${t.kind}`}
          onClick={() => {
            dismissToast(t.id);
            setOpen(true);
            markAnnouncementsSeen();
          }}
        >
          {t.message}
        </div>
      ))}
    </div>
  );

  if (!open) {
    return (
      <>
        {toastStack}
        <button
          className="jarvis-fab"
          onClick={() => {
            setOpen(true);
            markAnnouncementsSeen();
          }}
          title="Open JARVIS"
        >
          🤖 JARVIS
          {unseenCount > 0 && <span className="jarvis-badge">{unseenCount > 9 ? "9+" : unseenCount}</span>}
        </button>
      </>
    );
  }

  return (
    <>
      {toastStack}
      <div className="jarvis-panel">
        <div className="jarvis-header">
          <span>
            <span className={`status-dot ${connected ? "ok" : "bad"}`} />
            JARVIS
          </span>
          <button className="jarvis-close" onClick={() => setOpen(false)}>×</button>
        </div>

        {announcements.length > 0 && (
          <div className="jarvis-announcements">
            {announcements.slice(0, 3).map((a, i) => (
              <div key={i} className={`jarvis-announce jarvis-announce-${a.kind}`}>
                {a.message}
              </div>
            ))}
          </div>
        )}

        <div className="jarvis-messages" ref={scrollRef}>
          {messages.length === 0 && (
            <div className="jarvis-empty">
              Try: {suggested.map((s) => (
                <button key={s} className="jarvis-suggestion" onClick={() => submit(s)}>{s}</button>
              ))}
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`jarvis-msg jarvis-msg-${m.role}`}>
              {m.text}
              {m.needsConfirmation && <div className="jarvis-confirm-hint">Say it again to confirm.</div>}
            </div>
          ))}
          {sending && <div className="jarvis-msg jarvis-msg-jarvis jarvis-thinking">…</div>}
        </div>

        <form
          className="jarvis-input-row"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={pendingConfirmText ? `Confirm: ${pendingConfirmText}` : "Ask JARVIS…"}
          />
          {SPEECH_RECOGNITION && (
            <button
              type="button"
              className={`jarvis-mic ${listening ? "listening" : ""}`}
              onClick={toggleMic}
              title="Voice command"
            >
              🎙
            </button>
          )}
          <button type="submit" disabled={sending}>Send</button>
        </form>
      </div>
    </>
  );
}
