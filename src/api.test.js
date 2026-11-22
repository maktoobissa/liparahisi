import assert from "node:assert/strict";
import test from "node:test";

import { api, validateDocument, validateProductImage } from "./api.js";
import {
  formatDate,
  formatNumber,
  messages,
  supportedLanguages,
  translateKey,
  translatePlural,
} from "./translations.js";

test("API sends same-origin cookies and parses successful JSON", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    assert.deepEqual(await api("/health"), { ok: true });
    assert.equal(request.url, "/api/health");
    assert.equal(request.options.credentials, "same-origin");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("API reports server validation errors", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: "Faili si sahihi." }),
    { status: 422, headers: { "Content-Type": "application/json" } },
  );
  try {
    await assert.rejects(api("/documents"), /Faili si sahihi/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("upload validation enforces allowed formats and file size", () => {
  assert.doesNotThrow(() => validateProductImage({ type: "image/png", size: 1024 }));
  assert.throws(
    () => validateProductImage({ type: "text/plain", size: 10 }),
    /JPEG, PNG au WebP/,
  );
  assert.throws(
    () => validateProductImage({ type: "image/png", size: 5 * 1024 * 1024 + 1 }),
    /MB 5/,
  );
  assert.doesNotThrow(() => validateDocument({ type: "application/pdf", size: 1024 }));
  assert.throws(
    () => validateDocument({ type: "application/zip", size: 10 }),
    /PDF, JPEG, PNG au WebP/,
  );
  assert.throws(
    () => validateDocument({ type: "application/pdf", size: 10 * 1024 * 1024 + 1 }),
    /MB 10/,
  );
});

test("translation dictionaries have matching keys in all supported languages", () => {
  const expected = Object.keys(messages.en).sort();
  for (const language of supportedLanguages) {
    assert.deepEqual(Object.keys(messages[language]).sort(), expected, language);
  }
});

test("locale helpers format dates, numbers, and plural messages", () => {
  assert.match(formatNumber(1234.5, "fr"), /1.*234,5/);
  assert.match(formatDate("2026-03-08T12:00:00Z", "fr"), /2026/);
  assert.equal(translateKey("assistant.title", "fr"), "Assistant commercial");
  assert.equal(translatePlural("assistant.orders", 1, "en"), "1 order");
  assert.equal(translatePlural("assistant.orders", 2, "fr"), "2 commandes");
});
