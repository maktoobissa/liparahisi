import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { translate, useLanguage } from "./i18n.jsx";
import { api } from "./api.js";

const notificationContext = createContext(null);
const soundStorageKey = "liparahisi_notification_sound";
const inboxLimit = 30;

async function showDeviceNotification(title, body, tag) {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  const options = {
    body,
    icon: "/images/liparahisi-app-icon.svg",
    tag,
    data: { url: "/" },
  };
  const registration = "serviceWorker" in navigator
    ? await navigator.serviceWorker.getRegistration()
    : null;
  if (registration) {
    await registration.showNotification(title, options);
    return;
  }
  const systemNotification = new Notification(title, options);
  systemNotification.onclick = () => window.focus();
}

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
  const [inbox, setInbox] = useState([]);
  const [inboxOpen, setInboxOpen] = useState(false);
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

  const markAllRead = useCallback(() => {
    setInbox((current) => current.map((notification) => (
      notification.read ? notification : { ...notification, read: true }
    )));
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
      createdAt: now,
    };
    setInbox((current) => [...current.slice(-(inboxLimit - 1)), { ...toast, read: false }]);
    setToasts((current) => [...current.slice(-3), toast]);
    void playSound();
    if (permission === "granted" && typeof Notification !== "undefined") {
      showDeviceNotification(title, body, dedupeKey)
        .catch((error) => console.error("Could not show a system notification.", error));
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

  async function enableWebPush() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      notify("Web Push haipatikani kwenye kivinjari hiki.", "error");
      return false;
    }
    try {
      const config = await api("/config/push");
      if (!config.vapid_public_key) {
        notify("Web Push VAPID haijawekwa kwenye server.", "error");
        return false;
      }
      const registration = await navigator.serviceWorker.ready;
      const existing = await registration.pushManager.getSubscription();
      const subscription = existing || await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(config.vapid_public_key),
      });
      await api("/notifications/subscriptions", {
        method: "POST",
        body: JSON.stringify({
          channel: "WEB_PUSH",
          subscription: subscription.toJSON(),
        }),
      });
      notify("Web Push imewashwa.", "success");
      return true;
    } catch (error) {
      notify(error.message || "Imeshindikana kuwasha Web Push.", "error");
      return false;
    }
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = window.atob(base64);
    return Uint8Array.from([...raw].map((character) => character.charCodeAt(0)));
  }

  useEffect(() => {
    if (!inboxOpen) return undefined;
    function closeOnEscape(event) {
      if (event.key === "Escape") setInboxOpen(false);
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [inboxOpen]);

  const unreadCount = inbox.reduce((count, notification) => count + Number(!notification.read), 0);
  const dateLocale = language === "sw" ? "sw-TZ" : language === "fr" ? "fr-FR" : "en-TZ";
  const labels = {
    sw: {
      soundOn: "Sauti imewashwa", soundOff: "Washa sauti", system: "Washa arifa za kifaa",
      webPush: "Washa Web Push", granted: "Arifa za kifaa zimewashwa", denied: "Arifa zimezuiwa kwenye kivinjari",
      install: "Sakinisha app", close: "Funga arifa", open: "Fungua arifa",
      inbox: "Arifa", unread: (count) => `${count} hazijasomwa`,
      markRead: "Soma zote", empty: "Hakuna arifa bado.",
    },
    en: {
      soundOn: "Sound on", soundOff: "Turn sound on", system: "Enable device notifications",
      webPush: "Enable Web Push", granted: "Device notifications enabled", denied: "Notifications are blocked in browser",
      install: "Install app", close: "Dismiss notification", open: "Open notifications",
      inbox: "Notifications", unread: (count) => `${count} unread`,
      markRead: "Mark all as read", empty: "No notifications yet.",
    },
    fr: {
      soundOn: "Son activé", soundOff: "Activer le son", system: "Activer les notifications",
      webPush: "Activer Web Push", granted: "Notifications activées", denied: "Notifications bloquées par le navigateur",
      install: "Installer l'application", close: "Fermer la notification", open: "Ouvrir les notifications",
      inbox: "Notifications", unread: (count) => `${count} non lues`,
      markRead: "Tout marquer comme lu", empty: "Aucune notification pour le moment.",
    },
  }[language];

  return (
    <notificationContext.Provider value={{ notify, enableWebPush }}>
      {children}
      <aside
        className="notification-tools"
        aria-label={language === "sw" ? "Arifa na mipangilio" : language === "fr" ? "Notifications et paramètres" : "Notifications and settings"}
      >
        <button
          className="notification-center-trigger"
          type="button"
          aria-label={`${labels.open}${unreadCount ? `, ${labels.unread(unreadCount)}` : ""}`}
          aria-expanded={inboxOpen}
          aria-controls="notification-center"
          onClick={() => setInboxOpen((open) => !open)}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" />
          </svg>
          {unreadCount > 0 && <span className="notification-count">{unreadCount > 99 ? "99+" : unreadCount}</span>}
        </button>
        {inboxOpen && (
          <section className="notification-center" id="notification-center" aria-label={labels.inbox}>
            <header className="notification-center-header">
              <div>
                <strong>{labels.inbox}</strong>
                <span>{labels.unread(unreadCount)}</span>
              </div>
              {unreadCount > 0 && (
                <button type="button" onClick={markAllRead}>{labels.markRead}</button>
              )}
            </header>
            {inbox.length ? (
              <ul className="notification-center-list">
                {inbox.slice().reverse().map((notification) => (
                  <li className={notification.read ? "" : "unread"} key={notification.id}>
                    <span className="notification-center-dot" aria-hidden="true" />
                    <div>
                      <strong>{translate(notification.titleKey, language)}</strong>
                      <p>{translate(notification.message, language)}</p>
                      <time dateTime={new Date(notification.createdAt).toISOString()}>
                        {new Intl.DateTimeFormat(dateLocale, { dateStyle: "short", timeStyle: "short" }).format(notification.createdAt)}
                      </time>
                    </div>
                  </li>
                ))}
              </ul>
            ) : <p className="notification-center-empty">{labels.empty}</p>}
          </section>
        )}
        <button type="button" onClick={toggleSound} aria-pressed={soundEnabled}>
          {soundEnabled ? labels.soundOn : labels.soundOff}
        </button>
        {permission === "default" && (
          <button type="button" onClick={enableSystemNotifications}>{labels.system}</button>
        )}
        {permission === "granted" && <span>{labels.granted}</span>}
        {permission === "denied" && <span>{labels.denied}</span>}
        <button type="button" onClick={() => enableWebPush()}>{labels.webPush}</button>
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
