const API = "/api";
const productImageTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const documentTypes = new Set([
  "application/pdf",
  "image/jpeg",
  "image/png",
  "image/webp",
]);
const maxProductImageBytes = 5 * 1024 * 1024;
const maxDocumentBytes = 10 * 1024 * 1024;

function responseMessage(detail, fallback) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return fallback;
}

export async function api(path, options = {}) {
  const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
  const response = await fetch(`${API}${path}`, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body && !isFormData ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(responseMessage(result.detail, "Ombi halikukamilika."));
  return result;
}

export function validateProductImage(file) {
  if (!productImageTypes.has(file.type)) {
    throw new Error("Tumia picha ya JPEG, PNG au WebP.");
  }
  if (file.size > maxProductImageBytes) {
    throw new Error("Picha imezidi ukubwa wa MB 5.");
  }
}

export function validateDocument(file) {
  if (!documentTypes.has(file.type)) {
    throw new Error("Tumia PDF, JPEG, PNG au WebP.");
  }
  if (file.size > maxDocumentBytes) {
    throw new Error("Nyaraka imezidi ukubwa wa MB 10.");
  }
}
