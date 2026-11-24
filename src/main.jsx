import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { LanguageProvider } from "./i18n.jsx";
import { NotificationProvider } from "./notifications.jsx";
import "./styles.css";

class AppErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error) {
    console.error("LipaRahisi interface error:", error);
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return (
      <main className="app-error-fallback" role="alert">
        <img src="/images/liparahisi-logo.svg" alt="LipaRahisi" />
        <h1>Imeshindikana kufungua ukurasa</h1>
        <p>Taarifa za ukurasa hazijafikia kikamilifu. Jaribu kuonyesha ukurasa tena.</p>
        <button type="button" onClick={() => window.location.reload()}>Jaribu tena</button>
      </main>
    );
  }
}

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <AppErrorBoundary>
      <LanguageProvider>
        <NotificationProvider>
          <App />
        </NotificationProvider>
      </LanguageProvider>
    </AppErrorBoundary>
  </React.StrictMode>,
);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations()
    .then((registrations) => Promise.all(registrations.map((registration) => registration.unregister())))
    .then(() => {
      if ("caches" in window) {
        caches.keys()
          .then((keys) => Promise.all(keys.filter((key) => key.startsWith("liparahisi-")).map((key) => caches.delete(key))))
          .catch(() => {});
      }
    })
    .catch(() => {});
}
