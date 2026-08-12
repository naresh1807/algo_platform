import { create } from "zustand";

import { endpoints } from "../services/api.js";

/**
 * JARVIS panel state: the visible conversation, the live announcement
 * feed pushed over /ws/jarvis/live/ (manual 14.16), and the mic
 * listening flag. Kept separate from liveStore.js (market/signal/risk
 * pushes) since JARVIS is its own subsystem with its own socket and
 * its own request/response REST call, matching how apps/jarvis is a
 * separate Django app on the backend rather than folded into an
 * existing one.
 *
 * Announcements are pushed proactively by the backend -- they are not
 * a response to any command the user typed or said. So this store
 * surfaces them without requiring the panel to be open: `unseenCount`
 * drives the FAB badge, `toasts` drives a brief floating card near the
 * FAB, and high-priority kinds are spoken aloud immediately (see
 * `HIGH_PRIORITY_KINDS` below) -- matching manual 14.2's "voice ద్వారా
 * Platform మొత్తం Control చేయగలగాలి" vision and 14.16's "JARVIS
 * Announces" being something JARVIS does TO the user, not something
 * the user has to go ask for.
 */

// Spoken + always-toasted immediately, even with the panel closed.
// signal_generated is deliberately excluded -- it can fire every few
// minutes per watchlist symbol, and speaking/toasting every one of
// those would be noisy rather than helpful; it still lands in
// `announcements` for the panel's own feed when opened.
const HIGH_PRIORITY_KINDS = new Set([
  "market_open", "market_close", "trade_executed", "position_closed",
  "risk_warning", "risk_critical", "kill_switch",
  "ai_training_completed", "database_backup_completed", "drift_detected",
]);

export const useJarvisStore = create((set, get) => ({
  messages: [], // { role: 'user' | 'jarvis', text, route?, needsConfirmation? }
  announcements: [],
  toasts: [], // recent announcements shown near the FAB while the panel is closed
  unseenCount: 0,
  listening: false,
  sending: false,
  connected: false,
  pendingConfirmText: null, // set when JARVIS is waiting on a confirm

  addUserMessage: (text) => {
    set((state) => ({ messages: [...state.messages, { role: "user", text }] }));
  },

  // Hydrates past conversation from apps.jarvis.JarvisCommandHistory
  // (GET /jarvis/history/) so a page refresh doesn't wipe the visible
  // chat -- previously `messages` was pure client-side session state,
  // even though the backend has logged every exchange all along
  // (manual 14.25 "Explain Every Action"). Guarded on `messages.length
  // === 0` so this only ever seeds the FIRST render, never overwrites
  // messages the user has already sent/received this session (e.g. on
  // a second call from React StrictMode's dev double-invoke). Each
  // history row is one full exchange (raw_text + result_text), so it
  // expands to up to two chat bubbles, newest-last to read top-to-
  // bottom chronologically (the API itself returns newest-first).
  hydrateHistory: async () => {
    if (get().messages.length > 0) return;
    try {
      const response = await endpoints.jarvisHistory();
      const rows = [...(response.data.results ?? response.data ?? [])].reverse();
      const hydrated = [];
      for (const row of rows) {
        if (row.raw_text) hydrated.push({ role: "user", text: row.raw_text });
        if (row.result_text) {
          hydrated.push({ role: "jarvis", text: row.result_text, needsConfirmation: row.needs_confirmation });
        }
      }
      if (hydrated.length > 0 && get().messages.length === 0) {
        set({ messages: hydrated });
      }
    } catch {
      // History is a nice-to-have on load -- an empty chat (today's
      // prior behavior) is a fine degrade if this fails, not worth
      // surfacing an error for.
    }
  },

  markAnnouncementsSeen: () => set({ unseenCount: 0 }),

  dismissToast: (id) => {
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) }));
  },

  sendCommand: async (text, { inputMode = "text", confirm = false, navigate } = {}) => {
    if (!text.trim()) return;
    set({ sending: true });
    get().addUserMessage(text);
    try {
      const response = await endpoints.jarvisCommand(text, inputMode, confirm);
      const data = response.data;
      set((state) => ({
        messages: [
          ...state.messages,
          { role: "jarvis", text: data.text, route: data.route, needsConfirmation: data.needs_confirmation },
        ],
        pendingConfirmText: data.needs_confirmation ? text : null,
      }));
      if (data.route && data.route !== "__back__" && navigate) {
        navigate(data.route);
      } else if (data.route === "__back__" && navigate) {
        navigate(-1);
      }
      speak(data.text);
    } catch (err) {
      const detail = err.response?.data?.detail || "JARVIS couldn't reach the backend.";
      set((state) => ({ messages: [...state.messages, { role: "jarvis", text: detail }] }));
    } finally {
      set({ sending: false });
    }
  },

  connectAnnouncements: () => {
    if (get().connected) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${proto}://${window.location.host}/ws/jarvis/live/`);
    socket.onopen = () => set({ connected: true });
    socket.onclose = () => set({ connected: false });
    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      const toastId = `${Date.now()}-${Math.random()}`;
      set((state) => ({
        announcements: [data, ...state.announcements].slice(0, 30),
        unseenCount: state.unseenCount + 1,
        toasts: [...state.toasts, { ...data, id: toastId }].slice(-3),
      }));
      // Auto-dismiss the toast card on its own; the panel's own
      // `announcements` history (above) is unaffected and keeps it.
      setTimeout(() => get().dismissToast(toastId), 8000);

      if (HIGH_PRIORITY_KINDS.has(data.kind)) {
        speak(data.message);
      }
    };
  },

  setListening: (listening) => set({ listening }),
}));

// manual 14.2/14.7: Voice + Screen output. Browser's built-in
// SpeechSynthesis -- no external TTS API/key needed. Silently no-ops
// if unsupported (Safari/older browsers on some platforms), which is
// an honest degrade to text-only rather than a crash.
//
// Voice SELECTION matters far more than any rate/pitch tuning here --
// the "robotic" complaint this was fixed for was actually the OS only
// having old SAPI5 desktop voices installed (Microsoft David/Zira, US
// English) with no Indian voice at all, not a fixable code parameter.
// After installing Windows' en-IN OneCore voice pack (Microsoft Heera,
// Female / Microsoft Ravi, Male), _pickVoice() below prefers a real
// Indian female voice if the browser exposes one -- this is a runtime
// lookup against whatever's ACTUALLY installed, never a hardcoded
// voice name that might not exist on a different machine. Falls back
// step by step (en-IN female -> any en-IN -> any female-sounding name
// -> whatever the browser defaults to) rather than failing silently if
// the preferred voice isn't there.
let _cachedVoice = null;

function _pickVoice() {
  if (_cachedVoice) return _cachedVoice;
  const voices = window.speechSynthesis.getVoices();
  if (!voices.length) return null;

  _cachedVoice =
    voices.find((v) => v.lang === "en-IN" && /female|heera|neerja|priya/i.test(v.name)) ||
    voices.find((v) => v.lang === "en-IN") ||
    voices.find((v) => /female|zira|heera/i.test(v.name)) ||
    voices[0];
  return _cachedVoice;
}

if ("speechSynthesis" in window) {
  // Voices often load asynchronously after page load -- the cache gets
  // populated (or re-picked, if the list changes later e.g. a voice
  // pack finishes installing) once this fires, rather than speak()
  // permanently locking in whatever getVoices() returned on its very
  // first, possibly-empty call.
  window.speechSynthesis.onvoiceschanged = () => {
    _cachedVoice = null;
  };
}

function speak(text) {
  if (!("speechSynthesis" in window) || !text) return;
  try {
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    const voice = _pickVoice();
    if (voice) utterance.voice = voice;
    utterance.rate = 1.0;
    utterance.pitch = 1.05; // slightly higher than default -- reads as a younger/more natural female tone alongside the voice choice above, not a substitute for it
    window.speechSynthesis.speak(utterance);
  } catch {
    // Speech synthesis is a nice-to-have; never let it break the chat.
  }
}
