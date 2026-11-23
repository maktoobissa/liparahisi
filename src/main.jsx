import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { LanguageProvider, translate, useLanguage } from "./i18n.jsx";
import { NotificationProvider } from "./notifications.jsx";
import "./styles.css";

function AppShell() {
  const { language } = useLanguage();
  const [splashVisible, setSplashVisible] = useState(true);

  useEffect(() => {
    const timer = window.setTimeout(() => setSplashVisible(false), 450);
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <>
      <App />
      {splashVisible && (
        <div className="startup-screen" role="status">
          <img src="/images/liparahisi-logo.svg" alt="LipaRahisi" />
          <p>{translate("Inapakia mfumo...", language)}</p>
          <span className="startup-spinner" aria-hidden="true" />
        </div>
      )}
    </>
  );
}

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <LanguageProvider>
      <NotificationProvider>
        <AppShell />
      </NotificationProvider>
    </LanguageProvider>
  </React.StrictMode>,
);

if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((error) => {
      console.error("Could not register the LipaRahisi service worker.", error);
    });
  });
}
