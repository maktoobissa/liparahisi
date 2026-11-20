import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { translate, useLanguage } from "./i18n.jsx";

const notificationContext = createContext(null);
const soundStorageKey = "liparahisi_notification_sound";

export function useNotification() {
  const context = useContext(notificationContext);
  if (!context) throw new Error("NotificationProvider is required.");
  return context;
}

export function useFeedbackState(kind, initialValue = "") {
  const [value, setValue] = useState(initialValue);
  const valueRef = useRef(value);
  const { notify } = useNotification();
  useEffect(() => {
    valueRef.current = value;
  }, [value]);
  const setFeedback = useCallback((nextValue) => {
    const resolved = typeof nextValue === "function" ? nextValue(valueRef.current) : nextValue;
    valueRef.current = resolved;
    setValue(resolved);
    if (typeof resolved === "string" && resolved.trim()) notify(resolved, kind);
  }, [kind, notify]);
  return [value, setFeedback];
}

export function NotificationProvider({ children }) {
  const { language } = useLanguage();
  const [toasts, setToasts] = useState([]);
  const [soundEnabled, setSoundEnabled] = useState(() => {
    try {
      return window.localStorage.getItem(soundStorageKey) !== "false";
    } catch {
      return true;
    }
  });
  const [permission, setPermission] = useState(
    typeof Notification === "undefined" ? "unsupported" : Notification.permission,
  );
  const [installPrompt, setInstallPrompt] = useState(null);
  const [installError, setInstallError] = useState("");
  const audioContext = useRef(null);
  const pendingSound = useRef(false);
  const timers = useRef(new Map());
  const recentToasts = useRef(new Map());

  const playSound = useCallback(async (force = false) => {
    if (!soundEnabled && !force) return;
    try {
      const AudioContextType = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextType) return;
      if (!audioContext.current) audioContext.current = new AudioContextType();
      const context = audioContext.current;
      if (context.state === "suspended") await context.resume();
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(880, context.currentTime);
      oscillator.frequency.setValueAtTime(660, context.currentTime + 0.12);
      gain.gain.setValueAtTime(0.001, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.16, context.currentTime + 0.025);
      gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.28);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start();
      oscillator.stop(context.currentTime + 0.3);
    } catch (error) {
      if (!force) pendingSound.current = true;
      console.error("Could not play notification sound.", error);
    }
  }, [soundEnabled]);

  const dismiss = useCallback((id) => {
    const timer = timers.current.get(id);
    if (timer) window.clearTimeout(timer);
    timers.current.delete(id);
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const notify = useCallback((message, type = "info", options = {}) => {
    const body = translate(message, language);
    const dedupeKey = `${type}:${body}`;
    const now = Date.now();
    const previousTime = recentToasts.current.get(dedupeKey) || 0;
    if (now - previousTime < 10000) return;
    recentToasts.current.set(dedupeKey, now);
    for (const [key, time] of recentToasts.current) {
      if (now - time > 60000) recentToasts.current.delete(key);
    }
    const titleKey = options.title || (
      type === "error" ? "Hitilafu" : type === "success" ? "Imefanikiwa" : "Arifa"
    );
    const title = translate(titleKey, language);
    const toast = {
      id: `${now}-${Math.random().toString(36).slice(2)}`,
      type,
      titleKey,
      message,
    };
    setToasts((current) => [...current.slice(-3), toast]);
    void playSound();
    if (permission === "granted" && typeof Notification !== "undefined") {
      try {
        const systemNotification = new Notification(title, {
          body,
          icon: "/images/liparahisi-app-icon.svg",
          tag: dedupeKey,
        });
        systemNotification.onclick = () => window.focus();
      } catch (error) {
        console.error("Could not show a system notification.", error);
      }
    }
    const timer = window.setTimeout(() => dismiss(toast.id), 6500);
    timers.current.set(toast.id, timer);
  }, [dismiss, language, permission, playSound]);

  useEffect(() => {
    function unlockAudio() {
      if (!soundEnabled) return;
      try {
        const AudioContextType = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextType) return;
        if (!audioContext.current) audioContext.current = new AudioContextType();
        const context = audioContext.current;
        if (context.state === "suspended") {
          context.resume().then(() => {
            if (pendingSound.current) {
              pendingSound.current = false;
              void playSound(true);
            }
          }).catch((error) => console.error("Could not unlock notification audio.", error));
        }
      } catch (error) {
        console.error("Could not initialize notification audio.", error);
      }
    }
    document.addEventListener("pointerdown", unlockAudio);
    document.addEventListener("keydown", unlockAudio);
    return () => {
      document.removeEventListener("pointerdown", unlockAudio);
      document.removeEventListener("keydown", unlockAudio);
    };
  }, [playSound, soundEnabled]);

  useEffect(() => {
    const onInstallPrompt = (event) => {
      event.preventDefault();
      setInstallPrompt(event);
    };
    const onInstalled = () => {
      setInstallPrompt(null);
      setInstallError("");
    };
    window.addEventListener("beforeinstallprompt", onInstallPrompt);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onInstallPrompt);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  useEffect(() => () => {
    timers.current.forEach((timer) => window.clearTimeout(timer));
    timers.current.clear();
    if (audioContext.current && audioContext.current.state !== "closed") {
      audioContext.current.close().catch((error) => console.error("Could not close notification audio.", error));
    }
  }, []);

  function toggleSound() {
    const next = !soundEnabled;
    setSoundEnabled(next);
    try {
      window.localStorage.setItem(soundStorageKey, String(next));
    } catch (error) {
      console.error("Could not save notification sound preference.", error);
    }
    if (next) void playSound(true);
  }

  async function enableSystemNotifications() {
    if (typeof Notification === "undefined") {
      setPermission("unsupported");
      return;
    }
    try {
      setPermission(await Notification.requestPermission());
    } catch (error) {
      notify(error.message || "Arifa za kifaa hazikuweza kuwashwa.", "error");
    }
  }

  async function installApp() {
    if (!installPrompt) return;
    setInstallError("");
    try {
      await installPrompt.prompt();
      await installPrompt.userChoice;
      setInstallPrompt(null);
    } catch (error) {
      setInstallError(error.message || "Imeshindikana kusakinisha app.");
    }
  }

  const labels = {
    sw: { soundOn: "Sauti imewashwa", soundOff: "Washa sauti", system: "Washa arifa za kifaa", granted: "Arifa za kifaa zimewashwa", denied: "Arifa zimezuiwa kwenye kivinjari", install: "Sakinisha app", close: "Funga arifa" },
    en: { soundOn: "Sound on", soundOff: "Turn sound on", system: "Enable device notifications", granted: "Device notifications enabled", denied: "Notifications are blocked in browser", install: "Install app", close: "Dismiss notification" },
    fr: { soundOn: "Son activé", soundOff: "Activer le son", system: "Activer les notifications", granted: "Notifications activées", denied: "Notifications bloquées par le navigateur", install: "Installer l'application", close: "Fermer la notification" },
  }[language];

  return (
    <notificationContext.Provider value={{ notify }}>
      {children}
      <aside
        className="notification-tools"
        aria-label={language === "sw" ? "Mipangilio ya arifa" : language === "fr" ? "Paramètres de notification" : "Notification settings"}
      >
        <button type="button" onClick={toggleSound} aria-pressed={soundEnabled}>
          {soundEnabled ? labels.soundOn : labels.soundOff}
        </button>
        {permission === "default" && (
          <button type="button" onClick={enableSystemNotifications}>{labels.system}</button>
        )}
        {permission === "granted" && <span>{labels.granted}</span>}
        {permission === "denied" && <span>{labels.denied}</span>}
        {installPrompt && (
          <button type="button" onClick={() => installApp().catch((error) => setInstallError(error.message))}>
            {labels.install}
          </button>
        )}
        {installError && <span className="notification-tool-error" role="alert">{installError}</span>}
      </aside>
      <div className="notification-stack" aria-live="polite" aria-relevant="additions">
        {toasts.map((toast) => (
          <article className={`app-toast app-toast-${toast.type}`} key={toast.id} role={toast.type === "error" ? "alert" : "status"}>
            <div><strong>{translate(toast.titleKey, language)}</strong><span>{translate(toast.message, language)}</span></div>
            <button type="button" aria-label={labels.close} onClick={() => dismiss(toast.id)}>×</button>
          </article>
        ))}
      </div>
    </notificationContext.Provider>
  );
}
