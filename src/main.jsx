import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { LanguageProvider } from "./i18n.jsx";
import { NotificationProvider } from "./notifications.jsx";
import "./styles.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <LanguageProvider>
      <NotificationProvider>
        <App />
      </NotificationProvider>
    </LanguageProvider>
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
