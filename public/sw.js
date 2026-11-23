const CACHE_NAME = "liparahisi-shell-v9";
const APP_SHELL = [
  "/",
  "/manifest.webmanifest",
  "/images/liparahisi-logo.svg",
  "/images/liparahisi-app-icon.svg",
  "/images/payment-networks/mpesa.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key.startsWith("liparahisi-") && key !== CACHE_NAME)
          .map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            return caches.open(CACHE_NAME)
              .then((cache) => cache.put("/", response.clone())
                .catch((error) => console.error("Could not cache the app shell.", error)))
              .then(() => response);
          }
          return response;
        })
        .catch(async () => (await caches.match(request)) || (await caches.match("/"))),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (response.ok && response.type === "basic") {
          return caches.open(CACHE_NAME)
            .then((cache) => cache.put(request, response.clone())
              .catch((error) => console.error("Could not cache an app resource.", error)))
            .then(() => response);
        }
        return response;
      });
    }),
  );
});

self.addEventListener("push", (event) => {
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json();
    } catch {
      payload = { body: event.data.text() };
    }
  }
  const title = payload.title || "LipaRahisi";
  const options = {
    body: payload.body || "",
    icon: "/icons/icon.svg",
    badge: "/icons/icon.svg",
    data: { url: payload.link || "/" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      const appClient = clients.find((client) => new URL(client.url).origin === self.location.origin);
      if (appClient) return appClient.focus();
      return self.clients.openWindow(event.notification.data?.url || "/");
    }),
  );
});
