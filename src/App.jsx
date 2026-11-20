import React, { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import JsBarcode from "jsbarcode";
import { translate, useLanguage, useLocalize } from "./i18n.jsx";
import { useFeedbackState, useNotification } from "./notifications.jsx";

const API = "/api";
const ABLINER_PAYMENT_METHOD = "Abliner Mobile Money";
const productImageTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const maxProductImageBytes = 5 * 1024 * 1024;
const maxProductGalleryImages = 8;
const ablinerNetwork = {
  value: ABLINER_PAYMENT_METHOD,
  label: "Abliner Mobile Money",
  short: "AB",
  color: "bank",
};
const networks = [
  { value: "M-Pesa", label: "M-Pesa", short: "MP", color: "mpesa", logo: "/images/payment-networks/mpesa.svg" },
  { value: "Halo Pesa", label: "Halo Pesa", short: "HP", color: "halo", logo: "/images/payment-networks/halo-pesa.png" },
  { value: "Tigo Pesa", label: "Mixx by Yas", short: "TP", color: "tigo", logo: "/images/payment-networks/mixx-by-yas.jpg" },
  { value: "Airtel Money", label: "Airtel Money", short: "AM", color: "airtel", logo: "/images/payment-networks/airtel-money.png" },
  { value: "NMB", label: "NMB", short: "NB", color: "bank", logo: "/images/payment-networks/nmb.png" },
  { value: "CRDB", label: "CRDB", short: "CR", color: "bank", logo: "/images/payment-networks/crdb.png" },
  { value: "NBC", label: "NBC", short: "NC", color: "bank", logo: "/images/payment-networks/nbc.jpg" },
];
const clickpesaNetworkValues = new Set(["M-Pesa", "Halo Pesa", "Tigo Pesa", "Airtel Money"]);

function paymentOptionsFor(config) {
  if (config?.abliner_live_enabled !== true) return networks;
  const methods = config.demo_mode
    ? networks
    : config.live_payments_enabled
      ? networks.filter((method) => clickpesaNetworkValues.has(method.value))
      : [];
  return [...methods, ablinerNetwork];
}

function paymentMethodLabel(method) {
  return method === "Tigo Pesa" ? "Mixx by Yas" : method;
}

function formatMoney(amount) {
  let language = "sw";
  try {
    language = window.localStorage.getItem("liparahisi_language") || "sw";
  } catch {
    language = "sw";
  }
  const locale = language === "fr" ? "fr-FR" : language === "en" ? "en-TZ" : "sw-TZ";
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(Number(amount) || 0);
}

async function downloadReceiptPdf(receipt, language) {
  const t = (text) => translate(text, language);
  const { jsPDF } = await import("jspdf");
  const pdf = new jsPDF({ unit: "mm", format: "a4" });
  const margin = 16;
  const pageWidth = pdf.internal.pageSize.getWidth();
  const pageHeight = pdf.internal.pageSize.getHeight();
  let y = 28;

  function writeLine(text, { size = 10, bold = false, indent = 0, gap = 1 } = {}) {
    pdf.setFont("helvetica", bold ? "bold" : "normal");
    pdf.setFontSize(size);
    const lines = pdf.splitTextToSize(
      String(text ?? ""),
      pageWidth - margin * 2 - indent,
    );
    const height = Math.max(lines.length, 1) * 5;
    if (y + height > pageHeight - margin) {
      pdf.addPage();
      y = 19;
    }
    pdf.text(lines, margin + indent, y);
    y += height + gap;
  }

  const logoResponse = await fetch("/images/liparahisi-logo.svg");
  if (!logoResponse.ok) throw new Error("Imeshindikana kupakia logo ya LipaRahisi.");
  await pdf.addSvgAsImage(await logoResponse.text(), margin, 8, 58, 17.4);
  pdf.setTextColor(7, 27, 82);
  writeLine(t("RISITI YA MALIPO - DEMO"), { size: 11, bold: true, gap: 4 });
  writeLine(`${t("Oda:")} ${receipt.checkoutId}`, { bold: true });
  writeLine(`${t("Mnunuzi:")} ${receipt.buyerName}`);
  if (receipt.buyerEmail) writeLine(`${t("Barua pepe:")} ${receipt.buyerEmail}`);
  if (receipt.buyerPhone) writeLine(`${t("Simu:")} ${receipt.buyerPhone}`);
  writeLine(`${t("Njia ya malipo:")} ${receipt.paymentMethod}${receipt.bankBranch ? ` - ${receipt.bankBranch}` : ""}`);
  if (receipt.deliveryAddress) writeLine(`${t("Anuani ya delivery:")} ${receipt.deliveryAddress}`);
  writeLine(t("Hali: Imekamilika (DEMO)"), { bold: true, gap: 4 });

  for (const split of receipt.splits) {
    writeLine(split.shopName || t("Muuzaji"), { size: 12, bold: true, gap: 1 });
    for (const item of split.items) {
      const quantity = Number(item.quantity) || 0;
      const unitAmount = Number(item.unit_amount) || 0;
      writeLine(
        `${quantity} x ${item.product_name} @ TZS ${formatMoney(unitAmount)} = TZS ${formatMoney(quantity * unitAmount)}`,
        { indent: 3 },
      );
    }
    writeLine(`${t("Jumla ya bidhaa za muuzaji:")} TZS ${formatMoney(split.amount)}`, { bold: true });
    writeLine(`${t("Kamisheni ya jukwaa")} (${receipt.commissionPercent}%): TZS ${formatMoney(split.platformFee)}`);
    writeLine(
      `${t("Mgao wa muuzaji baada ya kamisheni")}: TZS ${formatMoney(split.sellerPayout)}`,
      { gap: 4 },
    );
  }

  if (y + 35 > pageHeight - margin) {
    pdf.addPage();
    y = 19;
  }
  pdf.setDrawColor(227, 234, 245);
  pdf.line(margin, y, pageWidth - margin, y);
  y += 7;
  writeLine(`${t("JUMLA YA MNUNUZI:")} TZS ${formatMoney(receipt.totalAmount)}`, { size: 12, bold: true });
  writeLine(`${t("Kamisheni jumla")} (${receipt.commissionPercent}%): TZS ${formatMoney(receipt.platformFee)}`, { bold: true });
  writeLine(`${t("Jumla ya mgao wa wauzaji:")} TZS ${formatMoney(receipt.sellerPayout)}`, { bold: true });
  writeLine(`${t("Tarehe:")} ${receipt.completedAt || "-"}`);
  writeLine(t("Risiti hii ni ya majaribio; hakuna pesa halisi iliyohamishwa."), { size: 9, gap: 0 });

  const filename = String(receipt.checkoutId || "oda").replace(/[^a-z0-9-]/gi, "");
  pdf.save(`risiti-${filename || "oda"}.pdf`);
}

function newRequestKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID().replace(/-/g, "");
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function responseMessage(detail, fallback) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return fallback;
}

async function api(path, options = {}) {
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

function validateProductImage(file) {
  if (!productImageTypes.has(file.type)) {
    throw new Error("Tumia picha ya JPEG, PNG au WebP.");
  }
  if (file.size > maxProductImageBytes) {
    throw new Error("Picha imezidi ukubwa wa MB 5.");
  }
}

async function uploadProductImage(productId, file) {
  validateProductImage(file);
  const formData = new FormData();
  formData.append("image", file);
  return api(`/products/${encodeURIComponent(productId)}/image`, {
    method: "POST",
    body: formData,
  });
}

async function uploadProductGallery(productId, files) {
  const formData = new FormData();
  files.forEach((file) => {
    validateProductImage(file);
    formData.append("images", file);
  });
  return api(`/products/${encodeURIComponent(productId)}/gallery`, {
    method: "POST",
    body: formData,
  });
}

function ProductGallery({ product, className = "", editable = false, onDeleteImage }) {
  const { language } = useLanguage();
  const images = product.gallery_images?.length
    ? product.gallery_images
    : product.image_url
      ? [{ id: null, url: product.image_url, primary: true }]
      : [];
  const [activeIndex, setActiveIndex] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const dragStart = useRef(null);
  const imageSignature = images.map((image) => `${image.id || ""}:${image.url}`).join("|");
  const t = (text) => translate(text, language);
  const multipleImages = images.length > 1;
  const activeImage = images[activeIndex] || images[0];

  useEffect(() => {
    setActiveIndex(0);
    setExpanded(false);
  }, [product.id, imageSignature]);

  useEffect(() => {
    if (!expanded) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function closeOnEscape(event) {
      if (event.key === "Escape") setExpanded(false);
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [expanded]);

  if (!activeImage) return null;

  function move(delta) {
    if (multipleImages) {
      setActiveIndex((index) => (index + delta + images.length) % images.length);
    }
  }

  function startDrag(event) {
    if (!multipleImages || event.target.closest("button")) return;
    dragStart.current = event.clientX;
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function finishDrag(event) {
    if (dragStart.current === null) return;
    const distance = event.clientX - dragStart.current;
    dragStart.current = null;
    if (Math.abs(distance) > 30) move(distance > 0 ? -1 : 1);
  }

  function handleKeyDown(event) {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      move(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      move(1);
    }
  }

  function renderFrame(isExpanded = false) {
    return (
      <div
        className={`product-gallery-frame ${multipleImages ? "is-rotatable" : ""} ${isExpanded ? "is-expanded" : ""}`}
        role="group"
        aria-label={t("Mionekano ya bidhaa")}
        tabIndex={multipleImages ? 0 : undefined}
        onKeyDown={handleKeyDown}
        onPointerDown={startDrag}
        onPointerUp={finishDrag}
        onPointerCancel={() => { dragStart.current = null; }}
      >
        <img
          src={activeImage.url}
          alt={`${product.name} (${activeIndex + 1}/${images.length})`}
          draggable="false"
        />
        {editable && activeImage.id && onDeleteImage && (
          <button
            className="product-gallery-delete"
            type="button"
            aria-label={`${t("Ondoa picha hii")} ${activeIndex + 1}`}
            onClick={() => onDeleteImage(product.id, activeImage.id)}
          >
            ×
          </button>
        )}
        {multipleImages && (
          <>
            <button
              className="product-gallery-arrow previous"
              type="button"
              aria-label={t("Picha iliyotangulia")}
              onClick={() => move(-1)}
            >
              ‹
            </button>
            <span className="product-gallery-count" aria-live="polite">
              {activeIndex + 1}/{images.length}
            </span>
            <button
              className="product-gallery-arrow next"
              type="button"
              aria-label={t("Picha inayofuata")}
              onClick={() => move(1)}
            >
              ›
            </button>
          </>
        )}
        {isExpanded ? (
          <button
            className="product-gallery-close"
            type="button"
            aria-label={t("Funga picha kubwa")}
            onClick={() => setExpanded(false)}
          >
            ×
          </button>
        ) : (
          <button
            className="product-gallery-expand"
            type="button"
            aria-label={t("Fungua picha kubwa")}
            onClick={() => setExpanded(true)}
          >
            ⤢
          </button>
        )}
        {multipleImages && (
          <div className="product-gallery-dots" aria-label={t("Chagua pembe ya bidhaa")}>
            {images.map((image, index) => (
              <button
                key={image.id || image.url}
                type="button"
                className={index === activeIndex ? "active" : ""}
                aria-label={`${t("Pembe")} ${index + 1}`}
                aria-current={index === activeIndex ? "true" : undefined}
                onClick={() => setActiveIndex(index)}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={`product-gallery ${className}`}>
      {renderFrame()}
      {multipleImages && (
        <p className="product-gallery-hint">{t("Buruta picha kushoto au kulia ili kuzungusha bidhaa.")}</p>
      )}
      {expanded && (
        <div className="product-gallery-backdrop" onClick={() => setExpanded(false)}>
          <div
            className="product-gallery-modal"
            role="dialog"
            aria-modal="true"
            aria-label={`${product.name} — ${t("Mionekano ya bidhaa")}`}
            onClick={(event) => event.stopPropagation()}
          >
            {renderFrame(true)}
            {multipleImages && (
              <p className="product-gallery-hint">{t("Buruta picha kushoto au kulia ili kuzungusha bidhaa.")}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function Header({ label = "Demo", user, onLogout }) {
  const { language, setLanguage } = useLanguage();
  const localize = useLocalize();
  return localize(
    <header className="topbar">
      <a className="brand" href="/" aria-label="LipaRahisi mwanzo" key="brand">
        <picture className="brand-image" key="brand-image">
          <source key="mobile-icon" media="(max-width: 440px)" srcSet="/images/liparahisi-app-icon.svg" />
          <img key="logo" className="brand-logo" src="/images/liparahisi-logo.svg" alt="" aria-hidden="true" />
        </picture>
        <span className="brand-mobile-name" key="brand-name">LipaRahisi</span>
      </a>
      <div className="header-right" key="controls">
        <label className="language-control" key="language">
          <span className="sr-only" key="language-label">Lugha</span>
          <select key="language-select" aria-label="Lugha" value={language} onChange={(event) => setLanguage(event.target.value)}>
            <option key="sw" value="sw">🇹🇿 Kiswahili</option>
            <option key="en" value="en">🇬🇧 English</option>
            <option key="fr" value="fr">🇫🇷 Français</option>
          </select>
        </label>
        <span className="prototype-label" key="label"><span className="status-dot" key="status-dot" />{label}</span>
        {user && (
          <button
            className="logout-button"
            type="button"
            onClick={onLogout}
            aria-label={`Toka kwenye akaunti ya ${user.full_name}`}
            key="logout"
          >
            Toka<span className="logout-name" key="logout-name"> ({user.full_name})</span>
          </button>
        )}
      </div>
    </header>
  );
}
function BrandFooter({ children }) {
  return (
    <footer className="site-footer">
      <a className="footer-brand" href="/" aria-label="LipaRahisi mwanzo">
        <img src="/images/liparahisi-app-icon.svg" alt="" aria-hidden="true" />
        <span>LipaRahisi</span>
      </a>
      <span className="footer-motto">Lipa <i>•</i> Tuma <i>•</i> Pokea</span>
      <div className="footer-content">{children}</div>
    </footer>
  );
}


function AuthScreen({ setupRequired, setupAvailable, onAuth, onBrowse }) {
  const localize = useLocalize();
  const [mode, setMode] = useState(setupRequired ? "setup" : "login");
  const [form, setForm] = useState({
    setup_token: "",
    full_name: "",
    email: "",
    password: "",
    shop_name: "",
    phone: "",
  });
  const [message, setMessage] = useFeedbackState("success");
  const [error, setError] = useFeedbackState("error");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (setupRequired) setMode("setup");
  }, [setupRequired]);

  function change(event) {
    setForm({ ...form, [event.target.name]: event.target.value });
  }

  async function submit(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    setBusy(true);
    try {
      if (mode === "setup") {
        if (!setupAvailable) throw new Error("Admin setup token haijawekwa. Wasiliana na msimamizi wa mfumo.");
        const result = await api("/auth/setup", {
          method: "POST",
          body: JSON.stringify({
            setup_token: form.setup_token,
            full_name: form.full_name,
            email: form.email,
            password: form.password,
          }),
        });
        onAuth(result.user);
      } else if (mode === "register" || mode === "buyer-register") {
        const isBuyer = mode === "buyer-register";
        const result = await api(isBuyer ? "/auth/customer/register" : "/auth/register", {
          method: "POST",
          body: JSON.stringify({
            full_name: form.full_name,
            email: form.email,
            password: form.password,
            ...(isBuyer ? {} : { shop_name: form.shop_name }),
            phone: form.phone || null,
          }),
        });
        setMessage(result.message);
        setMode("login");
      } else {
        const result = await api("/auth/login", {
          method: "POST",
          body: JSON.stringify({ email: form.email, password: form.password }),
        });
        onAuth(result.user);
      }
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  const isSetup = mode === "setup";
  const isRegister = mode === "register" || mode === "buyer-register";
  const isBuyerRegister = mode === "buyer-register";
  return localize(
    <main className="page-shell auth-shell">
      <Header label="Marketplace" />
      <section className="auth-card">
        <div className="eyebrow"><span /> {isSetup ? "USIMAMIZI MKUU" : isBuyerRegister ? "JIUNGE KAMA MNUNUZI" : isRegister ? "JIUNGE KAMA MUUZAJI" : "KARIBU TENA"}</div>
        <h1>{isSetup ? "Fungua akaunti ya admin" : isBuyerRegister ? "Fungua akaunti ya mnunuzi" : isRegister ? "Sajili duka lako" : "Ingia LipaRahisi"}</h1>
        <p className="buyer-intro">
          {isSetup
            ? "Akaunti hii itakuwa msimamizi mkuu wa marketplace na itaidhinisha wauzaji."
            : isBuyerRegister
              ? "Fungua akaunti ili kuhifadhi bidhaa, kuona oda zako na kuomba msaada."
              : isRegister
              ? "Baada ya kujisajili, admin ataidhinisha duka lako kabla bidhaa hazijaonekana."
              : "Wauzaji na msimamizi mkuu watumie akaunti zao kuingia."}
        </p>
        <form className="buyer-form" onSubmit={submit}>
          {isSetup && (
            <>
              <label className="field-label" htmlFor="setup-token">Tokeni ya kuanzisha admin</label>
              <input id="setup-token" className="text-input" name="setup_token" type="password" autoComplete="off" required value={form.setup_token} onChange={change} />
            </>
          )}
          {(isSetup || isRegister) && (
            <>
              <label className="field-label" htmlFor="full-name">Jina kamili</label>
              <input id="full-name" className="text-input" name="full_name" autoComplete="name" required minLength={2} value={form.full_name} onChange={change} />
            </>
          )}
          <label className="field-label" htmlFor="auth-email">Barua pepe</label>
          <input id="auth-email" className="text-input" name="email" type="email" autoComplete="email" required value={form.email} onChange={change} />
          {isRegister && (
            <>
              {!isBuyerRegister && (
                <>
                  <label className="field-label" htmlFor="shop-name">Jina la duka</label>
                  <input id="shop-name" className="text-input" name="shop_name" required minLength={2} value={form.shop_name} onChange={change} />
                </>
              )}
              <label className="field-label" htmlFor="seller-phone">Simu ya mawasiliano (hiari)</label>
              <input id="seller-phone" className="text-input" name="phone" type="tel" autoComplete="tel" value={form.phone} onChange={change} />
            </>
          )}
          <label className="field-label" htmlFor="auth-password">Nenosiri {isSetup || isRegister ? "(angalau herufi 10)" : ""}</label>
          <input id="auth-password" className="text-input" name="password" type="password" autoComplete={isSetup || isRegister ? "new-password" : "current-password"} minLength={isSetup || isRegister ? 10 : 1} required value={form.password} onChange={change} />
          {message && <p className="success-message" role="status">{message}</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="generate-button" type="submit" disabled={busy}>
            {busy ? "Inahifadhi..." : isSetup ? "Anzisha admin mkuu" : isRegister ? "Tuma usajili" : "Ingia"}
          </button>
        </form>
        {!setupRequired && mode === "login" && (
          <div className="auth-switch">
            <button type="button" onClick={() => { window.history.pushState({}, "", "/forgot-password"); window.location.reload(); }}>Umesahau nenosiri?</button>
          </div>
        )}
        {!setupRequired && (
          <div className="auth-switch">
            {isRegister ? "Tayari una akaunti?" : "Unataka kuuza bidhaa?"}
            <button type="button" onClick={() => { setError(""); setMessage(""); setMode(isRegister ? "login" : "register"); }}>
              {isRegister ? "Ingia" : "Jisajili kama muuzaji"}
            </button>
          </div>
        )}
        {!setupRequired && !isRegister && (
          <div className="auth-switch">
            Ununuzi binafsi?
            <button type="button" onClick={() => { setError(""); setMessage(""); setMode("buyer-register"); }}>Fungua akaunti ya mnunuzi</button>
          </div>
        )}
        {!setupRequired && isRegister && !isBuyerRegister && (
          <div className="auth-switch">
            Unanunua tu?
            <button type="button" onClick={() => { setError(""); setMessage(""); setMode("buyer-register"); }}>Jisajili kama mnunuzi</button>
          </div>
        )}
        {!setupRequired && <div className="auth-switch"><button type="button" onClick={onBrowse}>Rudi kuangalia bidhaa</button></div>}
      </section>
      <BrandFooter><span key="copyright">© 2026 LipaRahisi</span><span key="privacy">PIN au OTP haiombwi hapa</span></BrandFooter>
    </main>
  );
}

function ProductTile({
  product,
  onArchive,
  onUploadImage,
  onUploadGallery,
  onDeleteImage,
  onDeleteGalleryImage,
  canArchive,
}) {
  const localize = useLocalize();
  const [qr, setQr] = useState("");
  const barcodeRef = useRef(null);
  useEffect(() => {
    let active = true;
    QRCode.toDataURL(product.qr_url, {
      width: 320,
      margin: 1,
      errorCorrectionLevel: "M",
      color: { dark: "#071B52", light: "#FFFFFF" },
    }).then((image) => {
      if (active) setQr(image);
    }).catch(() => {});
    if (barcodeRef.current) {
      JsBarcode(barcodeRef.current, product.id, {
        format: "CODE128",
        lineColor: "#071B52",
        width: 1.5,
        height: 48,
        displayValue: true,
        font: "DM Sans",
        fontSize: 10,
        margin: 4,
      });
    }
    return () => { active = false; };
  }, [product.id, product.qr_url]);

  function downloadQr() {
    if (!qr) return;
    const link = document.createElement("a");
    link.href = qr;
    link.download = `${product.name.replace(/[^\p{L}\p{N}-]+/gu, "-")}-qr.png`;
    link.click();
  }

  function downloadBarcode() {
    if (!barcodeRef.current) return;
    const source = new XMLSerializer().serializeToString(barcodeRef.current);
    const link = document.createElement("a");
    const objectUrl = URL.createObjectURL(new Blob([source], { type: "image/svg+xml" }));
    link.href = objectUrl;
    link.download = `${product.name.replace(/[^\p{L}\p{N}-]+/gu, "-")}-barcode.svg`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }

  return localize(
    <article className={`product-tile ${product.is_active ? "" : "product-inactive"}`}>
      <ProductGallery
        product={product}
        className="product-card-gallery"
        editable={canArchive}
        onDeleteImage={onDeleteGalleryImage}
      />
      {qr ? <img className="product-qr" src={qr} alt={`QR ya ${product.name}`} /> : <div className="product-qr qr-placeholder" />}
      <div className="product-tile-details">
        <strong>{product.name}</strong>
        {product.category && <small>{product.category}</small>}
        {product.description && <small>{product.description}</small>}
        <span>TZS {formatMoney(product.amount)}</span>
        <small>Stock: {product.stock_quantity ?? 0}</small>
        {product.shop_name && <small>{product.shop_name}</small>}
        {!product.is_active && <small className="muted-status">Imeondolewa</small>}
        <div className="product-tile-actions">
          <button type="button" onClick={downloadQr} disabled={!qr}>Pakua QR</button>
          <button type="button" onClick={downloadBarcode}>Pakua barcode</button>
          {canArchive && (
            <label className="table-action">
              {product.image_url ? "Badilisha picha" : "Weka picha"}
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp"
                aria-label={`Pakia picha ya ${product.name}`}
                hidden
                onChange={(event) => {
                  const [file] = event.target.files || [];
                  if (file) onUploadImage(product.id, file);
                  event.target.value = "";
                }}
              />
            </label>
          )}
          {canArchive && (
            <label className="table-action">
              Ongeza pembe za picha
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp"
                aria-label="Ongeza pembe za picha"
                multiple
                hidden
                onChange={(event) => {
                  const files = Array.from(event.target.files || []);
                  if (files.length) onUploadGallery(product.id, files);
                  event.target.value = "";
                }}
              />
            </label>
          )}
          {canArchive && product.image_url && !product.image_url.includes("/gallery/") && (
            <button type="button" onClick={() => onDeleteImage(product.id)}>Ondoa picha kuu</button>
          )}
          {canArchive && product.is_active && <button type="button" onClick={() => onArchive(product.id)}>Ondoa</button>}
        </div>
      </div>
      <svg ref={barcodeRef} className="product-barcode" aria-label={`Barcode ya ${product.name}`} />
    </article>
  );
}

function StatCard({ title, value, note }) {
  const localize = useLocalize();
  return localize(
    <article className="stat-card">
      <span>{title}</span>
      <strong>{value}</strong>
      {note && <small>{note}</small>}
    </article>
  );
}

function MarketplaceDashboard({ user, onLogout }) {
  const localize = useLocalize();
  const { notify } = useNotification();
  const [tab, setTab] = useState("overview");
  const [stats, setStats] = useState(null);
  const [products, setProducts] = useState([]);
  const [sales, setSales] = useState([]);
  const [sellers, setSellers] = useState([]);
  const [auditRows, setAuditRows] = useState([]);
  const [categories, setCategories] = useState([]);
  const [lowStock, setLowStock] = useState([]);
  const [stockProductId, setStockProductId] = useState("");
  const [stockMovements, setStockMovements] = useState([]);
  const [cancellations, setCancellations] = useState([]);
  const [reviews, setReviews] = useState([]);
  const [tickets, setTickets] = useState([]);
  const [productForm, setProductForm] = useState({
    name: "", description: "", image_url: "", category: "", amount: "",
    cost_amount: "", stock_quantity: "", low_stock_threshold: "5",
  });
  const [productImages, setProductImages] = useState([]);
  const productImageInputRef = useRef(null);
  const [productQuery, setProductQuery] = useState("");
  const [salesFilters, setSalesFilters] = useState({ start_date: "", end_date: "", status: "" });
  const [categoryName, setCategoryName] = useState("");
  const [storefront, setStorefront] = useState({
    shop_name: user.shop_name || "",
    store_slug: user.store_slug || "",
    store_description: "",
    store_image_url: "",
  });
  const [csvText, setCsvText] = useState("");
  const [passwordForm, setPasswordForm] = useState({ current_password: "", new_password: "" });
  const [message, setMessage] = useFeedbackState("success");
  const [error, setError] = useFeedbackState("error");
  const [busy, setBusy] = useState(false);
  const isAdmin = user.role === "ADMIN";
  const seenSalesRef = useRef(null);
  const seenAdminEventsRef = useRef(null);

  function adminEventKeys(rows) {
    return rows.flatMap(([kind, records]) => records.map((record) => `${kind}:${record.id}`));
  }

  async function refresh() {
    const salesParams = new URLSearchParams(Object.fromEntries(
      Object.entries(salesFilters).filter(([, value]) => value),
    ));
    const [dashboard, productRows, salesRows] = await Promise.all([
      api("/dashboard"),
      api(`/products?q=${encodeURIComponent(productQuery)}`),
      api(`/sales?${salesParams.toString()}`),
    ]);
    setStats(dashboard);
    setProducts(productRows);
    setSales(salesRows);
    if (!seenSalesRef.current) {
      seenSalesRef.current = new Set(salesRows.map((sale) => sale.order_id));
    }
    setCategories(await api("/categories"));
    setLowStock(await api("/inventory/low-stock"));
    if (isAdmin) setAuditRows(await api("/admin/audit"));
    if (isAdmin) {
      const [sellerRows, cancellationRows, reviewRows, ticketRows] = await Promise.all([
        api("/admin/sellers"),
        api("/admin/cancellations"),
        api("/admin/reviews"),
        api("/admin/support-tickets"),
      ]);
      setSellers(sellerRows);
      setCancellations(cancellationRows);
      setReviews(reviewRows);
      setTickets(ticketRows);
      if (!seenAdminEventsRef.current) {
        seenAdminEventsRef.current = new Set(adminEventKeys([
          ["seller", sellerRows],
          ["cancellation", cancellationRows],
          ["review", reviewRows],
          ["ticket", ticketRows],
        ]));
      }
    }
  }

  useEffect(() => {
    refresh().catch((requestError) => setError(requestError.message));
  }, []);

  useEffect(() => {
    let active = true;
    let inFlight = false;
    async function refreshLiveSales() {
      if (!active || document.hidden || inFlight) return;
      inFlight = true;
      try {
        const params = new URLSearchParams(Object.entries(salesFilters).filter(([, value]) => value));
        const [dashboard, latestSales, adminData] = await Promise.all([
          api("/dashboard"),
          api(`/sales?${params.toString()}`),
          isAdmin ? Promise.all([
            api("/admin/sellers"),
            api("/admin/cancellations"),
            api("/admin/reviews"),
            api("/admin/support-tickets"),
          ]) : Promise.resolve(null),
        ]);
        if (active) {
          setStats(dashboard);
          setSales(latestSales);
          const knownSales = seenSalesRef.current || new Set();
          const newSales = latestSales.filter((sale) => !knownSales.has(sale.order_id));
          latestSales.forEach((sale) => knownSales.add(sale.order_id));
          seenSalesRef.current = knownSales;
          if (newSales.length) notify("Oda mpya imepokelewa.", "info");
          if (adminData) {
            const [sellerRows, cancellationRows, reviewRows, ticketRows] = adminData;
            setSellers(sellerRows);
            setCancellations(cancellationRows);
            setReviews(reviewRows);
            setTickets(ticketRows);
            const eventRows = [
              ["seller", sellerRows],
              ["cancellation", cancellationRows],
              ["review", reviewRows],
              ["ticket", ticketRows],
            ];
            const knownEvents = seenAdminEventsRef.current || new Set(adminEventKeys(eventRows));
            const currentKeys = adminEventKeys(eventRows);
            const hasNewEvents = currentKeys.some((key) => !knownEvents.has(key));
            currentKeys.forEach((key) => knownEvents.add(key));
            seenAdminEventsRef.current = knownEvents;
            if (hasNewEvents) notify("Kuna taarifa mpya za usimamizi.", "info");
          }
        }
      } catch (requestError) {
        if (active) setError(`Imeshindikana kusasisha taarifa za oda kiotomatiki: ${requestError.message}`);
      } finally {
        inFlight = false;
      }
    }
    const timer = window.setInterval(refreshLiveSales, 10000);
    document.addEventListener("visibilitychange", refreshLiveSales);
    return () => {
      active = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshLiveSales);
    };
  }, [isAdmin, notify, salesFilters]);

  useEffect(() => {
    if (isAdmin || !user.store_slug) return;
    api(`/storefronts/${encodeURIComponent(user.store_slug)}`)
      .then((result) => setStorefront({
        shop_name: result.store.shop_name,
        store_slug: result.store.store_slug,
        store_description: result.store.store_description,
        store_image_url: result.store.store_image_url,
      }))
      .catch((requestError) => setError(requestError.message));
  }, []);

  async function createProduct(event) {
    event.preventDefault();
    setMessage("");
    setError("");
    setBusy(true);
    try {
      const totalImages = productImages.length + Number(Boolean(productForm.image_url.trim()));
      if (totalImages > maxProductGalleryImages) {
        throw new Error(`Chagua picha zisizozidi ${maxProductGalleryImages} kwa bidhaa.`);
      }
      productImages.forEach(validateProductImage);
      const createdProduct = await api("/products", {
        method: "POST",
        body: JSON.stringify({
          name: productForm.name.trim(),
          description: productForm.description.trim(),
          image_url: productForm.image_url.trim(),
          category: productForm.category.trim(),
          amount: Number(productForm.amount),
          cost_amount: Number(productForm.cost_amount) || 0,
          stock_quantity: Number(productForm.stock_quantity) || 0,
          low_stock_threshold: Number(productForm.low_stock_threshold) || 0,
        }),
      });
      setProductForm({ name: "", description: "", image_url: "", category: "", amount: "", cost_amount: "", stock_quantity: "", low_stock_threshold: "5" });
      setProductImages([]);
      if (productImageInputRef.current) productImageInputRef.current.value = "";
      if (productImages.length) {
        try {
          await uploadProductGallery(createdProduct.id, productImages);
        } catch (uploadError) {
          setError(`Bidhaa imehifadhiwa, lakini picha hazikupakiwa: ${uploadError.message}`);
          await refresh();
          return;
        }
      }
      setMessage(productImages.length
        ? "Bidhaa na picha zake zimehifadhiwa."
        : "Bidhaa imehifadhiwa. QR na barcode yake vipo kwenye orodha hapa chini.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function uploadExistingProductImage(productId, file) {
    setError("");
    setMessage("");
    try {
      await uploadProductImage(productId, file);
      setMessage("Picha ya bidhaa imehifadhiwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function uploadExistingProductGallery(productId, files) {
    setError("");
    setMessage("");
    try {
      await uploadProductGallery(productId, files);
      setMessage("Picha za pembe za bidhaa zimehifadhiwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function deleteProductImage(productId) {
    setError("");
    setMessage("");
    try {
      await api(`/products/${encodeURIComponent(productId)}/image`, { method: "DELETE" });
      setMessage("Picha ya bidhaa imeondolewa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function deleteProductGalleryImage(productId, imageId) {
    setError("");
    setMessage("");
    try {
      await api(`/products/${encodeURIComponent(productId)}/gallery/${encodeURIComponent(imageId)}`, {
        method: "DELETE",
      });
      setMessage("Picha ya pembe ya bidhaa imeondolewa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function archiveProduct(productId) {
    setError("");
    try {
      await api(`/products/${productId}`, { method: "DELETE" });
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function archiveSale(orderId) {
    setError("");
    try {
      await api(`/sales/${orderId}/archive`, { method: "POST" });
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function changeSellerStatus(sellerId, status) {
    setError("");
    try {
      await api(`/admin/sellers/${sellerId}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      });
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function changePassword(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    try {
      await api("/auth/password", {
        method: "PUT",
        body: JSON.stringify(passwordForm),
      });
      setPasswordForm({ current_password: "", new_password: "" });
      setMessage("Nenosiri limebadilishwa.");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function updateDelivery(orderId, status, trackingCode) {
    await api(`/orders/${encodeURIComponent(orderId)}/delivery`, {
      method: "PUT",
      body: JSON.stringify({ delivery_status: status, tracking_code: trackingCode }),
    });
    await refresh();
  }

  async function loadMovements(productId) {
    setStockProductId(productId);
    try {
      setStockMovements(await api(`/products/${productId}/movements`));
      setError("");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function saveStorefront(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    try {
      const updated = await api("/storefront", {
        method: "PUT",
        body: JSON.stringify(storefront),
      });
      setStorefront(updated);
      setMessage("Taarifa za duka zimehifadhiwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function createCategory(event) {
    event.preventDefault();
    try {
      await api("/categories", {
        method: "POST",
        body: JSON.stringify({ name: categoryName }),
      });
      setCategoryName("");
      setMessage("Kundi limeongezwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function importCsv(event) {
    event.preventDefault();
    try {
      const result = await api("/products/import", {
        method: "POST",
        body: JSON.stringify({ csv_text: csvText }),
      });
      setCsvText("");
      setMessage(`Bidhaa ${result.imported_count} zimeingizwa.`);
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function uploadCsvFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > 1_000_000) {
      setError("Faili ya bidhaa imezidi 1 MB.");
      return;
    }
    try {
      setCsvText(await file.text());
      setMessage("CSV imepakiwa. Hakiki maandishi kisha chagua Ingiza CSV.");
      setError("");
    } catch (requestError) {
      setError(`Imeshindikana kusoma CSV: ${requestError.message}`);
    } finally {
      event.target.value = "";
    }
  }

  async function resolveCancellation(id, status) {
    try {
      await api(`/admin/cancellations/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status, admin_note: "" }),
      });
      setMessage(`Ombi la kughairi lime${status === "APPROVED" ? "idhinishwa" : "kataliwa"}.`);
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function moderateReview(id, status) {
    try {
      await api(`/admin/reviews/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      });
      setMessage("Hali ya maoni imehifadhiwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  const nav = [
    ["overview", "Muhtasari"],
    ["products", "Bidhaa"],
    ["sales", "Mauzo"],
    ...(!isAdmin ? [["storefront", "Duka lako"], ["stock", "Stock"]] : []),
    ...(isAdmin ? [["sellers", "Wauzaji"]] : []),
    ...(isAdmin ? [["categories", "Makundi"], ["cancellations", "Kughairi"], ["reviews", "Maoni"], ["support", "Msaada"]] : []),
    ...(isAdmin ? [["audit", "Audit"]] : []),
    ["settings", "Akaunti"],
  ];

  return localize(
    <main className="page-shell">
      <Header label={isAdmin ? "Admin mkuu" : "Dashboard ya muuzaji"} user={user} onLogout={onLogout} />
      <section className="dashboard-welcome">
        <div>
          <div className="eyebrow"><span /> {isAdmin ? "USIMAMIZI WA MARKETPLACE" : "AKAUNTI YA MUUZAJI"}</div>
          <h1>Habari, <span>{user.shop_name || user.full_name}</span></h1>
          <p>{isAdmin ? "Dhibiti wauzaji, bidhaa na rekodi zote za mauzo." : "Dhibiti bidhaa zako na fuatilia mapato ya duka lako."}</p>
        </div>
        <span className="demo-pill">Majaribio: hakuna pesa halisi</span>
      </section>
      <nav className="dashboard-tabs" aria-label="Sehemu za dashboard">
        {nav.map(([key, title]) => (
          <button key={key} type="button" className={tab === key ? "active" : ""} onClick={() => { setTab(key); setError(""); setMessage(""); }}>
            {title}
          </button>
        ))}
      </nav>
      {error && <p className="form-error dashboard-alert" role="alert">{error}</p>}
      {message && <p className="success-message dashboard-alert" role="status">{message}</p>}

      {tab === "overview" && stats && (
        <section className="dashboard-content">
          <div className="stats-grid">
            <StatCard title="Mauzo yaliyokamilika (demo)" value={`TZS ${formatMoney(stats.gross_sales)}`} />
            <StatCard title={isAdmin ? "Kamisheni ya admin (5%)" : "Kamisheni ya jukwaa (5%)"} value={`TZS ${formatMoney(stats.platform_fees)}`} />
            <StatCard title={isAdmin ? "Oda zilizokamilika" : "Faida halisi (demo)"} value={isAdmin ? stats.order_count : `TZS ${formatMoney(stats.net_profit)}`} note="Ada ya gateway haijajumuishwa kwenye demo." />
            <StatCard title="Bidhaa zilizo hai" value={stats.product_count} />
          </div>
          <div className="demo-notice dashboard-notice">
            <span className="notice-icon" aria-hidden="true">i</span>
            <p><strong>Hali ya majaribio.</strong> Rekodi za mapato/commission ni za demo; hakuna malipo yanayohamishwa. Ada halisi za mtandao hazijapimwa.</p>
          </div>
          <div className="dashboard-section-heading"><h2>Mauzo ya hivi karibuni</h2><button type="button" onClick={() => setTab("sales")}>Angalia yote</button></div>
          <SalesTable sales={sales.slice(0, 5)} isAdmin={isAdmin} onArchive={archiveSale} onUpdateDelivery={updateDelivery} />
        </section>
      )}

      {tab === "products" && (
        <section className="dashboard-content">
          {!isAdmin && (
            <div className="workspace dashboard-workspace">
              <form className="form-card" onSubmit={createProduct}>
                <div className="section-heading"><span className="step-number">01</span><div><h2>Ongeza bidhaa</h2><p>QR na barcode hutengenezwa kwa kila bidhaa.</p></div></div>
                <label className="field-label" htmlFor="product-name">Jina la bidhaa</label>
                <input id="product-name" className="text-input" required maxLength={80} value={productForm.name} onChange={(event) => setProductForm({ ...productForm, name: event.target.value })} />
                <label className="field-label" htmlFor="product-description">Maelezo</label>
                <textarea id="product-description" className="text-input feature-textarea" maxLength={2000} value={productForm.description} onChange={(event) => setProductForm({ ...productForm, description: event.target.value })} />
                <label className="field-label" htmlFor="product-image">URL ya picha (HTTP/HTTPS)</label>
                <input id="product-image" className="text-input" type="url" maxLength={2048} value={productForm.image_url} onChange={(event) => setProductForm({ ...productForm, image_url: event.target.value })} />
                <label className="field-label" htmlFor="product-image-file">Pakia picha za bidhaa (picha kuu na pembe tofauti)</label>
                <input
                  id="product-image-file"
                  ref={productImageInputRef}
                  className="text-input"
                  type="file"
                  accept="image/jpeg,image/png,image/webp"
                  multiple
                  onChange={(event) => setProductImages(Array.from(event.target.files || []))}
                />
                <p className="field-hint">Chagua hadi picha 8 za pembe tofauti. JPEG, PNG au WebP; kila picha hadi MB 5. Mnunuzi anaweza kuzizungusha.</p>
                <label className="field-label" htmlFor="product-category">Kundi</label>
                <input id="product-category" className="text-input" list="product-categories" maxLength={60} value={productForm.category} onChange={(event) => setProductForm({ ...productForm, category: event.target.value })} />
                <datalist id="product-categories">{categories.map((item) => <option key={item.id} value={item.name} />)}</datalist>
                <label className="field-label" htmlFor="product-amount">Bei ya kuuza (TZS)</label>
                <input id="product-amount" className="text-input" type="number" min="1" step="1" required value={productForm.amount} onChange={(event) => setProductForm({ ...productForm, amount: event.target.value })} />
                <label className="field-label" htmlFor="product-cost">Gharama ya bidhaa (TZS)</label>
                <input id="product-cost" className="text-input" type="number" min="0" step="1" value={productForm.cost_amount} onChange={(event) => setProductForm({ ...productForm, cost_amount: event.target.value })} />
                <label className="field-label" htmlFor="product-stock">Stock inayopatikana</label>
                <input id="product-stock" className="text-input" type="number" min="0" step="1" required value={productForm.stock_quantity} onChange={(event) => setProductForm({ ...productForm, stock_quantity: event.target.value })} />
                <label className="field-label" htmlFor="product-low-stock">Tahadhari stock ikiwa chini ya</label>
                <input id="product-low-stock" className="text-input" type="number" min="0" step="1" value={productForm.low_stock_threshold} onChange={(event) => setProductForm({ ...productForm, low_stock_threshold: event.target.value })} />
                <p className="field-hint">Gharama hutumika kukadiria faida; inaweza kuachwa wazi.</p>
                <button className="generate-button" type="submit" disabled={busy}>{busy ? "Inahifadhi..." : "Hifadhi bidhaa"}</button>
              </form>
              <aside className="result-card product-help">
                <h2>Barcode za bidhaa</h2>
                <p>Kila bidhaa ina QR inayofungua ukurasa wa malipo na barcode ya utambulisho wake.</p>
                <p>Pakua QR au barcode kutoka kwenye kadi ya bidhaa iliyo hapa chini.</p>
              </aside>
            </div>
          )}
          {!isAdmin && (
            <form className="form-card csv-import-form" onSubmit={importCsv}>
              <h2>Ingiza bidhaa kwa CSV</h2>
              <p className="field-hint">Safu zinazohitajika: <code>name,amount</code>. Hiari: description, image_url, category, cost_amount, stock_quantity, low_stock_threshold.</p>
              <label className="field-label" htmlFor="csv-file">Chagua CSV (kiwango cha juu 1 MB)</label>
              <input id="csv-file" className="text-input" type="file" accept=".csv,text/csv" onChange={uploadCsvFile} />
              <label className="field-label" htmlFor="csv-text">Maudhui ya CSV</label>
              <textarea id="csv-text" className="text-input feature-textarea" required value={csvText} onChange={(event) => setCsvText(event.target.value)} />
              <button className="generate-button" type="submit">Ingiza bidhaa</button>
              <a className="retry-link" href="/api/products/export">Pakua CSV ya bidhaa</a>
            </form>
          )}
          <div className="dashboard-section-heading"><h2>{isAdmin ? "Bidhaa zote" : "Bidhaa zako"}</h2><span>{products.filter((item) => item.is_active).length} hai</span></div>
          <div className="product-search">
            <input className="text-input" type="search" placeholder="Tafuta bidhaa au duka..." value={productQuery} onChange={(event) => setProductQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") refresh(); }} />
            <button type="button" className="table-action" onClick={() => refresh()}>Tafuta</button>
          </div>
          {products.length ? (
            <div className="product-grid">
              {products.map((product) => (
                <ProductTile
                  key={product.id}
                  product={product}
                  canArchive={!isAdmin}
                  onArchive={archiveProduct}
                  onUploadImage={uploadExistingProductImage}
                  onUploadGallery={uploadExistingProductGallery}
                  onDeleteImage={deleteProductImage}
                  onDeleteGalleryImage={deleteProductGalleryImage}
                />
              ))}
            </div>
          ) : <p className="empty-state">{isAdmin ? "Hakuna bidhaa bado." : "Bado hujaongeza bidhaa."}</p>}
        </section>
      )}

      {tab === "sales" && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading"><h2>{isAdmin ? "Rekodi zote za mauzo" : "Historia ya mauzo yako"}</h2><span>{sales.length} rekodi</span></div>
          <form className="product-search feature-search" onSubmit={(event) => { event.preventDefault(); refresh().catch((requestError) => setError(requestError.message)); }}>
            <label className="filter-field">Kuanzia<input className="text-input" type="date" value={salesFilters.start_date} onChange={(event) => setSalesFilters({ ...salesFilters, start_date: event.target.value })} /></label>
            <label className="filter-field">Mpaka<input className="text-input" type="date" value={salesFilters.end_date} onChange={(event) => setSalesFilters({ ...salesFilters, end_date: event.target.value })} /></label>
            <select className="text-input" aria-label="Hali ya oda" value={salesFilters.status} onChange={(event) => setSalesFilters({ ...salesFilters, status: event.target.value })}>
              <option value="">Hali zote</option><option value="PENDING">PENDING</option><option value="COMPLETED">COMPLETED</option><option value="EXPIRED">EXPIRED</option><option value="CANCELLED">CANCELLED</option>
            </select>
            <button type="submit" className="table-action">Chuja</button>
            <a className="table-action" href="/api/admin/export/sales">Pakua mauzo CSV</a>
          </form>
          <SalesTable sales={sales} isAdmin={isAdmin} onArchive={archiveSale} onUpdateDelivery={updateDelivery} />
          {!isAdmin && <p className="field-hint">Ukificha rekodi, itaondoka kwenye orodha yako lakini admin ataendelea kuiona.</p>}
        </section>
      )}

      {tab === "storefront" && !isAdmin && (
        <section className="dashboard-content feature-columns">
          <form className="form-card" onSubmit={saveStorefront}>
            <h2>Wasifu wa duka</h2>
            <label className="field-label" htmlFor="store-name">Jina la duka</label>
            <input id="store-name" className="text-input" minLength={2} maxLength={100} required value={storefront.shop_name} onChange={(event) => setStorefront({ ...storefront, shop_name: event.target.value })} />
            <label className="field-label" htmlFor="store-description">Maelezo ya duka</label>
            <textarea id="store-description" className="text-input feature-textarea" maxLength={1000} value={storefront.store_description} onChange={(event) => setStorefront({ ...storefront, store_description: event.target.value })} />
            <label className="field-label" htmlFor="store-image">URL ya picha ya duka</label>
            <input id="store-image" className="text-input" type="url" maxLength={2048} value={storefront.store_image_url} onChange={(event) => setStorefront({ ...storefront, store_image_url: event.target.value })} />
            <button className="generate-button" type="submit">Hifadhi duka</button>
          </form>
          <aside className="result-card product-help">
            <h2>Kiungo cha duka</h2>
            <p>Wateja wanaweza kuona bidhaa zako zote zilizo hai kwenye ukurasa mmoja.</p>
            {storefront.store_slug && <a className="table-action" href={`/store/${encodeURIComponent(storefront.store_slug)}`} target="_blank" rel="noreferrer">Fungua /store/{storefront.store_slug}</a>}
          </aside>
        </section>
      )}

      {tab === "stock" && !isAdmin && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading"><h2>Tahadhari ya stock</h2><span>{lowStock.length} bidhaa zinahitaji uangalizi</span></div>
          <div className="feature-list">
            {lowStock.map((product) => <article className="seller-card" key={product.id}><div><strong>{product.name}</strong><span>Stock: {product.stock_quantity} · Kiwango cha tahadhari: {product.low_stock_threshold}</span></div><button className="table-action" type="button" onClick={() => loadMovements(product.id)}>Historia ya stock</button></article>)}
            {!lowStock.length && <p className="empty-state">Hakuna bidhaa zilizo chini ya kiwango chako cha tahadhari.</p>}
          </div>
          <div className="dashboard-section-heading"><h2>Bidhaa na mabadiliko ya stock</h2></div>
          <div className="feature-list">{products.map((product) => <article className="seller-card" key={product.id}><div><strong>{product.name}</strong><span>Stock sasa: {product.stock_quantity}</span></div><button className="table-action" type="button" onClick={() => loadMovements(product.id)}>Angalia historia</button></article>)}</div>
          {stockProductId && <div className="table-scroll"><h3>Mabadiliko ya stock</h3><table className="sales-table"><thead><tr><th>Mabadiliko</th><th>Sababu</th><th>Tarehe</th></tr></thead><tbody>{stockMovements.map((item) => <tr key={item.id}><td>{item.quantity_delta > 0 ? "+" : ""}{item.quantity_delta}</td><td>{item.reason}</td><td>{item.created_at}</td></tr>)}{!stockMovements.length && <tr><td colSpan="3">Hakuna mabadiliko yaliyorekodiwa.</td></tr>}</tbody></table></div>}
        </section>
      )}

      {tab === "categories" && isAdmin && (
        <section className="dashboard-content">
          <form className="form-card inline-feature-form" onSubmit={createCategory}>
            <label className="field-label" htmlFor="category-name">Jina la kundi jipya</label>
            <input id="category-name" className="text-input" minLength={2} maxLength={60} required value={categoryName} onChange={(event) => setCategoryName(event.target.value)} />
            <button className="generate-button" type="submit">Ongeza kundi</button>
          </form>
          <div className="feature-list">{categories.map((item) => <article className="seller-card" key={item.id}><strong>{item.name}</strong></article>)}</div>
        </section>
      )}

      {tab === "cancellations" && isAdmin && (
        <section className="dashboard-content feature-list">
          <h2>Maombi ya kughairi oda</h2>
          {cancellations.map((item) => <article className="seller-card" key={item.id}><div><strong>{item.order_id} · {item.buyer_name}</strong><span>{item.reason} · TZS {formatMoney(item.amount)}</span><small>{item.status} · {item.payment_status}</small></div>{item.status === "PENDING" && <div className="seller-actions"><button type="button" onClick={() => resolveCancellation(item.id, "APPROVED")}>Idhinisha</button><button type="button" className="danger-action" onClick={() => resolveCancellation(item.id, "REJECTED")}>Kataa</button></div>}</article>)}
          {!cancellations.length && <p className="empty-state">Hakuna ombi la kughairi.</p>}
          <p className="field-hint">Kughairi oda iliyokamilika ni demo pekee: stock hurejeshwa, lakini refund ya pesa halisi haijatekelezwa.</p>
        </section>
      )}

      {tab === "reviews" && isAdmin && (
        <section className="dashboard-content feature-list">
          <h2>Ukaguzi wa maoni</h2>
          {reviews.map((item) => <article className="seller-card" key={item.id}><div><strong>{item.product_name} · {item.rating}/5</strong><span>{item.buyer_name} · {item.order_id}</span><small>{item.comment || "Hakuna maelezo"} · {item.status}</small></div>{item.status === "PENDING" && <div className="seller-actions"><button type="button" onClick={() => moderateReview(item.id, "APPROVED")}>Idhinisha</button><button type="button" className="danger-action" onClick={() => moderateReview(item.id, "REJECTED")}>Kataa</button></div>}</article>)}
          {!reviews.length && <p className="empty-state">Hakuna maoni yaliyowasilishwa.</p>}
        </section>
      )}

      {tab === "support" && isAdmin && (
        <section className="dashboard-content feature-list">
          <h2>Maombi ya msaada</h2>
          {tickets.map((item) => <AdminSupportTicket key={item.id} ticket={item} onSave={async (payload) => {
            try {
              await api(`/admin/support-tickets/${item.id}`, { method: "PATCH", body: JSON.stringify(payload) });
              await refresh();
              setMessage("Ombi la msaada limesasishwa.");
            } catch (requestError) {
              setError(requestError.message);
            }
          }} />)}
          {!tickets.length && <p className="empty-state">Hakuna maombi ya msaada.</p>}
        </section>
      )}

      {tab === "sellers" && isAdmin && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading"><h2>Wauzaji waliosajiliwa</h2><span>{sellers.length} jumla</span></div>
          <div className="seller-list">
            {sellers.map((seller) => (
              <article className="seller-card" key={seller.id}>
                <div><strong>{seller.shop_name}</strong><span>{seller.full_name} · {seller.email}</span><small>{seller.phone || "Hakuna simu"} · Mauzo: TZS {formatMoney(seller.gross_sales)} · Commission: TZS {formatMoney(seller.platform_fees)}</small></div>
                <span className={`seller-status ${seller.status.toLowerCase()}`}>{seller.status}</span>
                <div className="seller-actions">
                  {seller.status !== "ACTIVE" && <button type="button" onClick={() => changeSellerStatus(seller.id, "ACTIVE")}>Idhinisha</button>}
                  {seller.status === "ACTIVE" && <button type="button" onClick={() => changeSellerStatus(seller.id, "SUSPENDED")}>Sitisha</button>}
                  {seller.status !== "REJECTED" && <button type="button" className="danger-action" onClick={() => changeSellerStatus(seller.id, "REJECTED")}>Kataa</button>}
                </div>
              </article>
            ))}
            {!sellers.length && <p className="empty-state">Hakuna muuzaji aliyesajiliwa bado.</p>}
          </div>
        </section>
      )}

      {tab === "audit" && isAdmin && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading">
            <h2>Rekodi za usalama na shughuli</h2>
            {isAdmin && <a className="table-action" href="/api/admin/export/sales">Pakua mauzo CSV</a>}
          </div>
          {auditRows.length ? (
            <div className="table-scroll">
              <table className="sales-table">
                <thead><tr><th>Shughuli</th><th>Mtumiaji</th><th>Target</th><th>Maelezo</th><th>IP</th><th>Muda</th></tr></thead>
                <tbody>
                  {auditRows.map((row) => (
                    <tr key={row.id}>
                      <td><strong>{row.action}</strong></td>
                      <td>{row.actor_email || "Mfumo"}</td>
                      <td>{row.target_type}: {row.target_id}</td>
                      <td>{row.detail}</td>
                      <td>{row.ip_address || "—"}</td>
                      <td>{row.created_at}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <p className="empty-state">Bado hakuna rekodi za audit.</p>}
        </section>
      )}

      {tab === "settings" && (
        <section className="dashboard-content">
          <form className="form-card settings-form" onSubmit={changePassword}>
            <h2>Badilisha nenosiri</h2>
            <label className="field-label" htmlFor="current-password">Nenosiri la sasa</label>
            <input id="current-password" className="text-input" type="password" autoComplete="current-password" required value={passwordForm.current_password} onChange={(event) => setPasswordForm({ ...passwordForm, current_password: event.target.value })} />
            <label className="field-label" htmlFor="new-password">Nenosiri jipya (angalau herufi 10)</label>
            <input id="new-password" className="text-input" type="password" autoComplete="new-password" minLength={10} required value={passwordForm.new_password} onChange={(event) => setPasswordForm({ ...passwordForm, new_password: event.target.value })} />
            <button className="generate-button" type="submit">Hifadhi nenosiri</button>
          </form>
        </section>
      )}

      <BrandFooter><span key="copyright">© 2026 LipaRahisi</span><span key="privacy">Data ya akaunti na mauzo hutenganishwa kwa muuzaji</span></BrandFooter>
    </main>
  );
}

function SalesTable({ sales, isAdmin, onArchive, onUpdateDelivery }) {
  const localize = useLocalize();
  if (!sales.length) return localize(<p className="empty-state">Hakuna mauzo yanayoonekana bado.</p>);
  return localize(
    <div className="table-scroll">
      <table className="sales-table">
        <thead><tr><th>Oda / Bidhaa</th>{isAdmin && <th>Muuzaji</th>}<th>Mnunuzi</th><th>Malipo</th><th>Delivery</th><th>Kiasi</th><th>Commission</th>{!isAdmin && <th />}</tr></thead>
        <tbody>
          {sales.map((sale) => <SaleRow key={sale.order_id} sale={sale} isAdmin={isAdmin} onArchive={onArchive} onUpdateDelivery={onUpdateDelivery} />)}
        </tbody>
      </table>
    </div>
  );
}

function SaleRow({ sale, isAdmin, onArchive, onUpdateDelivery }) {
  const localize = useLocalize();
  const [deliveryStatus, setDeliveryStatus] = useState(sale.delivery_status || "UNFULFILLED");
  const [trackingCode, setTrackingCode] = useState(sale.tracking_code || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useFeedbackState("error");
  async function saveDelivery() {
    setBusy(true);
    setError("");
    try {
      await onUpdateDelivery(sale.order_id, deliveryStatus, trackingCode);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }
  return localize(
    <tr className={sale.archived_at ? "archived-row" : ""}>
      <td><strong>{sale.product_name}</strong><small>{sale.order_id}</small></td>
      {isAdmin && <td>{sale.shop_name || "—"}</td>}
      <td>{sale.buyer_name || "—"}<small>{paymentMethodLabel(sale.payment_method || "")}</small>{sale.delivery_address && <small>{sale.delivery_address}</small>}</td>
      <td><span className={`order-status ${sale.payment_status.toLowerCase()}`}>{sale.payment_status === "COMPLETED" ? "Imekamilika (demo)" : sale.payment_status}</span>{sale.archived_at && <small>Imefichwa na muuzaji</small>}</td>
      <td>
        {sale.payment_status === "COMPLETED" && onUpdateDelivery ? (
          <div className="delivery-editor">
            <select className="text-input" aria-label="Hali ya delivery" value={deliveryStatus} onChange={(event) => setDeliveryStatus(event.target.value)}>
              <option value="UNFULFILLED">Haijaanza</option><option value="PROCESSING">Inaandaliwa</option><option value="SHIPPED">Imesafirishwa</option><option value="DELIVERED">Imefikishwa</option>
            </select>
            <input className="text-input" aria-label="Namba ya ufuatiliaji" maxLength={100} placeholder="Tracking code" value={trackingCode} onChange={(event) => setTrackingCode(event.target.value)} />
            <button type="button" className="table-action" disabled={busy || deliveryStatus === "UNFULFILLED"} onClick={saveDelivery}>{busy ? "..." : "Hifadhi"}</button>
            {error && <small className="form-error">{error}</small>}
          </div>
        ) : <span>{sale.delivery_status || "UNFULFILLED"}</span>}
      </td>
      <td>TZS {formatMoney(sale.amount)}</td>
      <td>TZS {formatMoney(sale.platform_fee)}</td>
      {!isAdmin && <td>{sale.payment_status === "COMPLETED" && !sale.archived_at && <button className="table-action" type="button" onClick={() => onArchive(sale.order_id)}>Ficha</button>}</td>}
    </tr>
  );
}

function AdminSupportTicket({ ticket, onSave }) {
  const localize = useLocalize();
  const [status, setStatus] = useState(ticket.status);
  const [response, setResponse] = useState(ticket.admin_response || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useFeedbackState("error");
  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onSave({ status, admin_response: response });
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }
  return localize(
    <article className="seller-card">
      <div><strong>{ticket.subject}</strong><span>{ticket.email} · {ticket.created_at}</span><small>{ticket.message}</small></div>
      <form className="ticket-response-form" onSubmit={submit}>
        <select className="text-input" aria-label="Hali ya ombi" value={status} onChange={(event) => setStatus(event.target.value)}><option value="OPEN">OPEN</option><option value="IN_PROGRESS">IN_PROGRESS</option><option value="RESOLVED">RESOLVED</option></select>
        <textarea className="text-input feature-textarea" aria-label="Jibu la admin" maxLength={4000} value={response} onChange={(event) => setResponse(event.target.value)} />
        <button className="table-action" type="submit" disabled={busy}>{busy ? "Inahifadhi..." : "Hifadhi jibu"}</button>
        {error && <small className="form-error">{error}</small>}
      </form>
    </article>
  );
}

function PublicMarketplace({ onLogin }) {
  const localize = useLocalize();
  const [products, setProducts] = useState([]);
  const [categories, setCategories] = useState([]);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [error, setError] = useFeedbackState("error");

  async function refresh() {
    const params = new URLSearchParams();
    if (query.trim()) params.set("q", query.trim());
    if (category) params.set("category", category);
    const [productRows, categoryRows] = await Promise.all([
      api(`/products?${params.toString()}`),
      api("/categories"),
    ]);
    setProducts(productRows);
    setCategories(categoryRows);
  }

  useEffect(() => {
    refresh().catch((requestError) => setError(requestError.message));
  }, []);

  async function search(event) {
    event.preventDefault();
    try {
      await refresh();
      setError("");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  return localize(
    <main className="page-shell">
      <Header label="Marketplace" />
      <section className="dashboard-welcome">
        <div><div className="eyebrow"><span /> MARKETPLACE</div><h1>Nunua kwa <span>LipaRahisi</span></h1><p>Tafuta bidhaa, angalia maduka na anza oda ya majaribio.</p></div>
        <button className="table-action" type="button" onClick={onLogin}>Ingia / Jisajili</button>
      </section>
      <form className="product-search feature-search" onSubmit={search}>
        <input className="text-input" type="search" placeholder="Tafuta bidhaa au duka..." value={query} onChange={(event) => setQuery(event.target.value)} />
        <select className="text-input" value={category} onChange={(event) => setCategory(event.target.value)}>
          <option value="">Makundi yote</option>
          {categories.map((item) => <option key={item.id} value={item.name}>{item.name}</option>)}
        </select>
        <button className="table-action" type="submit">Tafuta</button>
      </form>
      {error && <p className="form-error dashboard-alert" role="alert">{error}</p>}
      {products.length ? (
        <div className="product-grid marketplace-grid">
          {products.map((product) => (
            <article className="market-product" key={product.id}>
              <ProductGallery product={product} className="market-product-gallery" />
              <div><small>{product.category || "Bidhaa"}</small><h2>{product.name}</h2><p>{product.description || "Bidhaa ya muuzaji wetu."}</p><strong>TZS {formatMoney(product.amount)}</strong><span>{product.shop_name || "Duka"}</span></div>
              <div className="market-product-actions">
                {product.store_slug && <a className="table-action" href={`/store/${encodeURIComponent(product.store_slug)}`}>Tembelea duka</a>}
                <a className="generate-button" href={`/pay/${product.id}`}>Nunua sasa</a>
              </div>
            </article>
          ))}
        </div>
      ) : <p className="empty-state">Hakuna bidhaa za kuonyesha kwa sasa.</p>}
      <BrandFooter><span key="copyright">© 2026 LipaRahisi</span><span key="payments">Malipo halisi hayajaunganishwa</span></BrandFooter>
    </main>
  );
}

function StorefrontPage({ storeSlug }) {
  const localize = useLocalize();
  const [storefront, setStorefront] = useState(null);
  const [error, setError] = useFeedbackState("error");
  useEffect(() => {
    api(`/storefronts/${encodeURIComponent(storeSlug)}`)
      .then(setStorefront)
      .catch((requestError) => setError(requestError.message));
  }, [storeSlug]);

  return localize(
    <main className="page-shell">
      <Header label="Duka la muuzaji" />
      {error && <p className="form-error dashboard-alert">{error}</p>}
      {!storefront && !error && <p className="buyer-intro">Inapakia duka...</p>}
      {storefront && (
        <>
          <section className="dashboard-welcome storefront-welcome">
            {storefront.store.store_image_url && <img src={storefront.store.store_image_url} alt="" />}
            <div><div className="eyebrow"><span /> DUKA LILILOIDHINISHWA</div><h1>{storefront.store.shop_name}</h1><p>{storefront.store.store_description || `Duka la ${storefront.store.full_name}`}</p></div>
          </section>
          <div className="product-grid marketplace-grid">
            {storefront.products.map((product) => (
              <article className="market-product" key={product.id}>
                <ProductGallery product={product} className="market-product-gallery" />
                <div><small>{product.category || "Bidhaa"}</small><h2>{product.name}</h2><p>{product.description}</p><strong>TZS {formatMoney(product.amount)}</strong></div>
                <a className="generate-button" href={`/pay/${product.id}`}>Nunua sasa</a>
              </article>
            ))}
            {!storefront.products.length && <p className="empty-state">Duka hili halina bidhaa hai kwa sasa.</p>}
          </div>
        </>
      )}
      <BrandFooter><a className="retry-link" href="/" key="back">Rudi marketplace</a><span key="payments">Malipo ya majaribio</span></BrandFooter>
    </main>
  );
}

function BuyerDashboard({ user, onLogout }) {
  const localize = useLocalize();
  const { notify } = useNotification();
  const [tab, setTab] = useState("browse");
  const [products, setProducts] = useState([]);
  const [categories, setCategories] = useState([]);
  const [wishlist, setWishlist] = useState([]);
  const [orders, setOrders] = useState([]);
  const [tickets, setTickets] = useState([]);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [ticket, setTicket] = useState({ subject: "", message: "" });
  const [error, setError] = useFeedbackState("error");
  const [message, setMessage] = useFeedbackState("success");
  const [busy, setBusy] = useState(false);
  const previousOrdersRef = useRef(null);
  const previousTicketsRef = useRef(null);

  function orderStatusMap(rows) {
    return new Map(rows.map((order) => [
      order.order_id,
      `${order.payment_status}:${order.delivery_status || ""}`,
    ]));
  }

  function ticketResponseMap(rows) {
    return new Map(rows.map((item) => [item.id, item.admin_response || ""]));
  }

  async function refresh() {
    const [productRows, categoryRows, wishlistRows, orderRows, ticketRows] = await Promise.all([
      api("/products"),
      api("/categories"),
      api("/wishlist"),
      api("/orders/history"),
      api("/support-tickets"),
    ]);
    setProducts(productRows);
    setCategories(categoryRows);
    setWishlist(wishlistRows);
    setOrders(orderRows);
    setTickets(ticketRows);
    if (!previousOrdersRef.current) previousOrdersRef.current = orderStatusMap(orderRows);
    if (!previousTicketsRef.current) previousTicketsRef.current = ticketResponseMap(ticketRows);
  }

  useEffect(() => {
    refresh().catch((requestError) => setError(requestError.message));
  }, []);

  useEffect(() => {
    let active = true;
    let inFlight = false;
    async function refreshOrderAndSupportUpdates() {
      if (!active || document.hidden || inFlight) return;
      inFlight = true;
      try {
        const [orderRows, ticketRows] = await Promise.all([
          api("/orders/history"),
          api("/support-tickets"),
        ]);
        if (!active) return;
        const knownOrders = previousOrdersRef.current;
        const currentOrders = orderStatusMap(orderRows);
        const changedOrders = knownOrders
          ? orderRows.filter((order) => knownOrders.has(order.order_id)
            && knownOrders.get(order.order_id) !== currentOrders.get(order.order_id))
          : [];
        if (changedOrders.length) notify("Hali ya oda imebadilika.", "info");
        previousOrdersRef.current = currentOrders;

        const knownTickets = previousTicketsRef.current;
        const currentTickets = ticketResponseMap(ticketRows);
        const receivedResponses = knownTickets
          ? ticketRows.filter((item) => item.admin_response
            && knownTickets.get(item.id) !== item.admin_response)
          : [];
        if (receivedResponses.length) notify("Timu ya msaada imejibu ombi lako.", "info");
        previousTicketsRef.current = currentTickets;
        setOrders(orderRows);
        setTickets(ticketRows);
      } catch (requestError) {
        if (active) setError(requestError.message);
      } finally {
        inFlight = false;
      }
    }
    const timer = window.setInterval(refreshOrderAndSupportUpdates, 10000);
    document.addEventListener("visibilitychange", refreshOrderAndSupportUpdates);
    return () => {
      active = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshOrderAndSupportUpdates);
    };
  }, [notify, setError]);

  async function runAction(action, successMessage) {
    setError("");
    setMessage("");
    try {
      await action();
      setMessage(successMessage);
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function submitTicket(event) {
    event.preventDefault();
    setBusy(true);
    await runAction(
      () => api("/support-tickets", {
        method: "POST",
        body: JSON.stringify({ email: user.email, ...ticket }),
      }),
      "Ombi lako la msaada limetumwa.",
    );
    setTicket({ subject: "", message: "" });
    setBusy(false);
  }

  const filteredProducts = products.filter((product) =>
    (!category || product.category === category)
    && (!query || `${product.name} ${product.shop_name || ""}`.toLowerCase().includes(query.toLowerCase()))
  );
  const tabs = [["browse", "Bidhaa"], ["wishlist", "Vipendwa"], ["orders", "Oda zangu"], ["support", "Msaada"]];

  return localize(
    <main className="page-shell">
      <Header label="Akaunti ya mnunuzi" user={user} onLogout={onLogout} />
      <section className="dashboard-welcome"><div><div className="eyebrow"><span /> AKAUNTI YA MNUNUZI</div><h1>Habari, <span>{user.full_name}</span></h1><p>Vinjari bidhaa, fuatilia oda na wasiliana na timu ya msaada.</p></div><a className="table-action" href="/">Marketplace ya umma</a></section>
      <nav className="dashboard-tabs" aria-label="Sehemu za mnunuzi">
        {tabs.map(([key, title]) => <button key={key} type="button" className={tab === key ? "active" : ""} onClick={() => { setTab(key); setError(""); setMessage(""); }}>{title}</button>)}
      </nav>
      {error && <p className="form-error dashboard-alert" role="alert">{error}</p>}
      {message && <p className="success-message dashboard-alert" role="status">{message}</p>}
      {tab === "browse" && (
        <section className="dashboard-content">
          <form className="product-search feature-search" onSubmit={(event) => event.preventDefault()}>
            <input className="text-input" type="search" placeholder="Tafuta bidhaa au duka..." value={query} onChange={(event) => setQuery(event.target.value)} />
            <select className="text-input" value={category} onChange={(event) => setCategory(event.target.value)}><option value="">Makundi yote</option>{categories.map((item) => <option key={item.id} value={item.name}>{item.name}</option>)}</select>
          </form>
          <div className="product-grid marketplace-grid">
            {filteredProducts.map((product) => (
              <article className="market-product" key={product.id}>
                <ProductGallery product={product} className="market-product-gallery" />
                <div><small>{product.category || "Bidhaa"}</small><h2>{product.name}</h2><p>{product.description}</p><strong>TZS {formatMoney(product.amount)}</strong><span>{product.shop_name || "Duka"}</span></div>
                <div className="market-product-actions">
                  <button type="button" className="table-action" onClick={() => runAction(() => api(`/wishlist/${product.id}`, { method: "POST" }), "Bidhaa imehifadhiwa kwenye vipendwa.")}>Hifadhi</button>
                  {product.store_slug && <a className="table-action" href={`/store/${encodeURIComponent(product.store_slug)}`}>Duka</a>}
                  <a className="generate-button" href={`/pay/${product.id}`}>Nunua</a>
                </div>
              </article>
            ))}
            {!filteredProducts.length && <p className="empty-state">Hakuna bidhaa zinazolingana na utafutaji.</p>}
          </div>
        </section>
      )}
      {tab === "wishlist" && (
        <section className="dashboard-content">
          <div className="product-grid marketplace-grid">
            {wishlist.map((product) => (
              <article className="market-product" key={product.id}>
                <ProductGallery product={product} className="market-product-gallery" />
                <div><small>{product.category || "Bidhaa"}</small><h2>{product.name}</h2><strong>TZS {formatMoney(product.amount)}</strong><span>{product.shop_name}</span></div>
                <div className="market-product-actions"><button type="button" className="table-action" onClick={() => runAction(() => api(`/wishlist/${product.id}`, { method: "DELETE" }), "Bidhaa imeondolewa kwenye vipendwa.")}>Ondoa</button><a className="generate-button" href={`/pay/${product.id}`}>Nunua</a></div>
              </article>
            ))}
            {!wishlist.length && <p className="empty-state">Hujahifadhi bidhaa yoyote bado.</p>}
          </div>
        </section>
      )}
      {tab === "orders" && (
        <section className="dashboard-content">
          <div className="table-scroll"><table className="sales-table">
            <thead><tr><th>Oda</th><th>Bidhaa / Duka</th><th>Malipo</th><th>Usafirishaji</th><th>Kiasi</th><th>Vitendo</th></tr></thead>
            <tbody>{orders.map((order) => <BuyerOrderRow key={order.order_id} order={order} onAction={runAction} />)}</tbody>
          </table></div>
          {!orders.length && <p className="empty-state">Bado huna oda zinazohusishwa na akaunti hii.</p>}
          <p className="field-hint">Oda ulizoweka bila kuingia haziwezi kuunganishwa na historia ya akaunti.</p>
        </section>
      )}
      {tab === "support" && (
        <section className="dashboard-content feature-columns">
          <form className="form-card" onSubmit={submitTicket}>
            <h2>Tuma ombi la msaada</h2>
            <label className="field-label" htmlFor="support-subject">Mada</label><input id="support-subject" className="text-input" minLength={3} maxLength={120} required value={ticket.subject} onChange={(event) => setTicket({ ...ticket, subject: event.target.value })} />
            <label className="field-label" htmlFor="support-message">Ujumbe</label><textarea id="support-message" className="text-input feature-textarea" minLength={10} maxLength={4000} required value={ticket.message} onChange={(event) => setTicket({ ...ticket, message: event.target.value })} />
            <button className="generate-button" type="submit" disabled={busy}>{busy ? "Inatuma..." : "Tuma ombi"}</button>
          </form>
          <div className="feature-list"><h2>Ombi zako</h2>{tickets.map((item) => <article className="seller-card" key={item.id}><div><strong>{item.subject}</strong><span>{item.status} · {item.created_at}</span><small>{item.admin_response || item.message}</small></div></article>)}{!tickets.length && <p className="empty-state">Hakuna ombi la msaada bado.</p>}</div>
        </section>
      )}
      <BrandFooter><span key="copyright">© 2026 LipaRahisi</span><span key="payments">Malipo ya majaribio pekee</span></BrandFooter>
    </main>
  );
}

function BuyerOrderRow({ order, onAction }) {
  const localize = useLocalize();
  const [reason, setReason] = useState("");
  const [rating, setRating] = useState("5");
  const [comment, setComment] = useState("");
  const canCancel = ["PENDING", "COMPLETED"].includes(order.payment_status);
  return localize(
    <tr>
      <td><strong>{order.order_id}</strong><small>{order.created_at}</small></td>
      <td>{order.product_name}<small>{order.shop_name || "Muuzaji"}</small></td>
      <td>{order.payment_status}</td>
      <td>{order.delivery_status}{order.tracking_code && <small>{order.tracking_code}</small>}{order.delivery_address && <small>{order.delivery_address}</small>}</td>
      <td>TZS {formatMoney(order.amount)}</td>
      <td>
        <a className="table-action" href={`/pay/${order.product_id}?order_id=${encodeURIComponent(order.order_id)}`}>Fungua</a>
        {canCancel && <form className="inline-feature-form" onSubmit={(event) => { event.preventDefault(); onAction(() => api(`/orders/${order.order_id}/cancellation`, { method: "POST", body: JSON.stringify({ reason }) }), "Ombi la kughairi limetumwa."); }}><input aria-label="Sababu ya kughairi" className="text-input" minLength={5} placeholder="Sababu ya kughairi" required value={reason} onChange={(event) => setReason(event.target.value)} /><button className="table-action" type="submit">Omba kughairi</button></form>}
        {order.payment_status === "COMPLETED" && <form className="inline-feature-form" onSubmit={(event) => { event.preventDefault(); onAction(() => api(`/orders/${order.order_id}/products/${order.product_id}/review`, { method: "POST", body: JSON.stringify({ rating: Number(rating), comment }) }), "Maoni yametumwa kwa ukaguzi."); }}><select aria-label="Alama" className="text-input" value={rating} onChange={(event) => setRating(event.target.value)}>{[5, 4, 3, 2, 1].map((value) => <option key={value} value={value}>{value} / 5</option>)}</select><input aria-label="Maoni kuhusu bidhaa" className="text-input" maxLength={1000} placeholder="Maoni (hiari)" value={comment} onChange={(event) => setComment(event.target.value)} /><button className="table-action" type="submit">Tuma maoni</button></form>}
      </td>
    </tr>
  );
}

function ForgotPasswordPage() {
  const localize = useLocalize();
  const [email, setEmail] = useState("");
  const [message, setMessage] = useFeedbackState("success");
  const [error, setError] = useFeedbackState("error");
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    setBusy(true);
    try {
      const result = await api("/auth/password-reset/request", {
        method: "POST",
        body: JSON.stringify({ email }),
      });
      setMessage(result.message);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }
  return localize(
    <main className="page-shell auth-shell">
      <Header label="Password reset" />
      <section className="auth-card">
        <div className="eyebrow"><span /> AKAUNTI</div>
        <h1>Rejesha nenosiri</h1>
        <form className="buyer-form" onSubmit={submit}>
          <label className="field-label" htmlFor="forgot-email">Barua pepe</label>
          <input id="forgot-email" className="text-input" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} />
          {message && <p className="success-message" role="status">{message}</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="generate-button" type="submit" disabled={busy}>{busy ? "Inatuma..." : "Tuma link ya reset"}</button>
        </form>
        <a className="retry-link" href="/">Rudi kuingia</a>
      </section>
    </main>
  );
}


function ResetPasswordPage() {
  const localize = useLocalize();
  const [password, setPassword] = useState("");
  const [message, setMessage] = useFeedbackState("success");
  const [error, setError] = useFeedbackState("error");
  const [busy, setBusy] = useState(false);
  const token = new URLSearchParams(window.location.search).get("token") || "";
  async function submit(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    setBusy(true);
    try {
      const result = await api("/auth/password-reset/confirm", {
        method: "POST",
        body: JSON.stringify({ reset_token: token, new_password: password }),
      });
      setMessage(result.message);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }
  return localize(
    <main className="page-shell auth-shell">
      <Header label="Password reset" />
      <section className="auth-card">
        <div className="eyebrow"><span /> AKAUNTI</div>
        <h1>Weka nenosiri jipya</h1>
        <form className="buyer-form" onSubmit={submit}>
          <label className="field-label" htmlFor="reset-password">Nenosiri jipya (angalau herufi 10)</label>
          <input id="reset-password" className="text-input" type="password" minLength={10} required value={password} onChange={(event) => setPassword(event.target.value)} />
          {message && <p className="success-message" role="status">{message}</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="generate-button" type="submit" disabled={busy || !token}>{busy ? "Inahifadhi..." : "Badilisha nenosiri"}</button>
        </form>
        <a className="retry-link" href="/">Rudi kuingia</a>
      </section>
    </main>
  );
}


function VerifyEmailPage() {
  const localize = useLocalize();
  const [message, setMessage] = useFeedbackState("success", "Inathibitisha...");
  const [error, setError] = useFeedbackState("error");
  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("token") || "";
    if (!token) {
      setError("Token ya uthibitisho haipo.");
      setMessage("");
      return;
    }
    api(`/auth/verify-email?token=${encodeURIComponent(token)}`)
      .then((result) => setMessage(result.message))
      .catch((requestError) => { setError(requestError.message); setMessage(""); });
  }, []);
  return localize(
    <main className="page-shell auth-shell">
      <Header label="Email verification" />
      <section className="auth-card">
        <h1>Thibitisha barua pepe</h1>
        {message && <p className="success-message" role="status">{message}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <a className="retry-link" href="/">Rudi mwanzo</a>
      </section>
    </main>
  );
}


const CART_STORAGE_KEY = "liparahisi_cart";

function loadCart() {
  try {
    return JSON.parse(window.localStorage.getItem(CART_STORAGE_KEY) || "[]")
      .filter((item) => item?.product_id && Number(item.quantity) > 0);
  } catch {
    return [];
  }
}

function saveCart(items) {
  window.localStorage.setItem(CART_STORAGE_KEY, JSON.stringify(items));
}

function CheckoutPage({ productId }) {
  const localize = useLocalize();
  const { language } = useLanguage();
  const { notify } = useNotification();
  const [product, setProduct] = useState(null);
  const [paymentConfig, setPaymentConfig] = useState(null);
  const [form, setForm] = useState({ buyer_name: "", buyer_email: "", buyer_phone: "", delivery_address: "", payment_method: "M-Pesa" });
  const [orderId, setOrderId] = useState(new URLSearchParams(window.location.search).get("order_id") || "");
  const [order, setOrder] = useState(null);
  const previousOrderStatus = useRef(null);
  const [error, setError] = useFeedbackState("error");
  const [busy, setBusy] = useState(false);
  const [cartCount, setCartCount] = useState(() => loadCart().reduce((sum, item) => sum + item.quantity, 0));

  useEffect(() => {
    api(`/products/${productId}`).then(setProduct).catch((requestError) => setError(requestError.message));
  }, [productId]);

  useEffect(() => {
    let active = true;
    api("/config/integrations")
      .then((config) => {
        if (active) {
          setPaymentConfig(config);
          if (
            config.abliner_live_enabled === true
            && config.demo_mode !== true
            && config.live_payments_enabled !== true
          ) {
            setForm((current) => current.payment_method === "M-Pesa"
              ? { ...current, payment_method: ABLINER_PAYMENT_METHOD }
              : current);
          }
        }
      })
      .catch((requestError) => {
        if (active) setError(requestError.message);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!orderId) return;
    let active = true;
    let timer;
    async function refreshOrderStatus() {
      try {
        const status = await api(`/orders/${encodeURIComponent(orderId)}`);
        if (!active) return;
        if (previousOrderStatus.current === "PENDING" && status.payment_status === "COMPLETED") {
          notify("Malipo ya oda yamekamilika.", "success");
        }
        previousOrderStatus.current = status.payment_status;
        setOrder(status);
        if (status.payment_status === "PENDING") {
          timer = window.setTimeout(refreshOrderStatus, 5000);
        }
      } catch (requestError) {
        if (active) {
          setError(requestError.message);
          timer = window.setTimeout(refreshOrderStatus, 5000);
        }
      }
    }
    refreshOrderStatus();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [notify, orderId]);

  function addToCart() {
    const cart = loadCart();
    const existing = cart.find((item) => item.product_id === productId);
    if (existing) existing.quantity += 1;
    else cart.push({ product_id: productId, quantity: 1 });
    saveCart(cart);
    setCartCount(cart.reduce((sum, item) => sum + item.quantity, 0));
  }

  async function submitCheckout(event) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const result = await api(`/products/${productId}/checkout`, {
        method: "POST",
        body: JSON.stringify({
          buyer_name: form.buyer_name,
          buyer_email: form.buyer_email,
          buyer_phone: form.buyer_phone,
          delivery_address: form.delivery_address,
          payment_method: form.payment_method,
          request_key: newRequestKey(),
        }),
      });
      if (!result.order_id) throw new Error("Mfumo haukurudisha namba ya oda.");
      setOrderId(result.order_id);
      window.history.replaceState(
        {},
        "",
        `/pay/${productId}?order_id=${encodeURIComponent(result.order_id)}${result.demo ? "&demo=1" : ""}`,
      );
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function completeDemo() {
    setError("");
    setBusy(true);
    try {
      const result = await api(`/orders/${encodeURIComponent(orderId)}/demo-complete`, { method: "POST" });
      const status = await api(`/orders/${encodeURIComponent(result.order_id)}`);
      setOrder(status);
      if (status.payment_status === "COMPLETED") notify("Malipo ya oda yamekamilika.", "success");
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  function retryPayment() {
    setOrderId("");
    setOrder(null);
    setError("");
    previousOrderStatus.current = null;
    window.history.replaceState({}, "", `/pay/${productId}`);
  }

  async function downloadOrderReceipt() {
    if (!order || order.payment_status !== "COMPLETED") return;
    const amount = Number(order.amount) || 0;
    const platformFee = Number(order.platform_fee) || 0;
    await downloadReceiptPdf({
      checkoutId: order.order_id,
      buyerName: order.buyer_name || form.buyer_name || "Mgeni",
      paymentMethod: paymentMethodLabel(order.payment_method),
      bankBranch: order.bank_branch,
      deliveryAddress: order.delivery_address,
      totalAmount: amount,
      platformFee,
      sellerPayout: Number(order.seller_payout) || 0,
      commissionPercent: order.commission_percent,
      completedAt: order.completed_at,
      splits: [{
        shopName: product?.shop_name || "Muuzaji",
        amount,
        platformFee,
        sellerPayout: Number(order.seller_payout) || 0,
        items: [{ product_name: order.product_name, quantity: 1, unit_amount: amount }],
      }],
    }, language);
  }

  const isCompleted = order?.payment_status === "COMPLETED";
  const isLivePayment = paymentConfig?.live_payments_enabled === true;
  const isAblinerLive = paymentConfig?.abliner_live_enabled === true;
  const isAblinerSelected = form.payment_method === ABLINER_PAYMENT_METHOD;
  const paymentMethods = paymentOptionsFor(paymentConfig);
  const selectedMethod = networks.find((method) => method.value === form.payment_method);
  return localize(
    <main className="page-shell checkout-shell">
      <Header label="Malipo ya bidhaa" />
      <div className="checkout-layout">
        {!product && !error ? <p className="buyer-intro">Inapakia bidhaa...</p> : null}
        {error && (!orderId || !order) && <div className="checkout-card"><p className="form-error" role="alert">{error}</p><a className="retry-link" href="/">Rudi mwanzo</a></div>}
        {product && !orderId && (
          <section className="checkout-card">
            <div className="eyebrow"><span /> BIDHAA IMETAMBULIKA</div>
            <h1>{product.name}</h1>
            <ProductGallery product={product} className="checkout-product-gallery" />
            <div className="checkout-amount">TZS {formatMoney(product.amount)}</div>
            <p className="buyer-intro">Jaza taarifa za oda na uchague njia ya malipo. PIN au OTP haiingizwe kwenye ukurasa huu.</p>
            <form className="buyer-form" onSubmit={submitCheckout}>
              <label className="field-label" htmlFor="buyer-name">Jina la mnunuzi</label>
              <input id="buyer-name" className="text-input" autoComplete="name" required minLength={2} value={form.buyer_name} onChange={(event) => setForm({ ...form, buyer_name: event.target.value })} />
              <label className="field-label" htmlFor="buyer-email">Barua pepe ya risiti</label>
              <input id="buyer-email" className="text-input" type="email" autoComplete="email" required value={form.buyer_email} onChange={(event) => setForm({ ...form, buyer_email: event.target.value })} />
              <label className="field-label" htmlFor="buyer-phone">Namba ya simu</label>
              <input id="buyer-phone" className="text-input" type="tel" inputMode="tel" autoComplete="tel" placeholder="Mf. 0712 345 678" required value={form.buyer_phone} onChange={(event) => setForm({ ...form, buyer_phone: event.target.value })} />
              <label className="field-label" htmlFor="buyer-address">Anuani ya delivery (hiari)</label>
              <input id="buyer-address" className="text-input" maxLength={500} autoComplete="street-address" value={form.delivery_address} onChange={(event) => setForm({ ...form, delivery_address: event.target.value })} />
              {isAblinerSelected && (
                <p className="field-hint">Abliner itatuma ombi la malipo kwenye simu yako. Thibitisha kwenye simu; usiweke PIN au OTP kwenye tovuti hii.</p>
              )}
              {isLivePayment && !isAblinerSelected && (
                <p className="field-hint">ClickPesa itatuma ombi la USSD kwenye namba uliyoingiza. Ada za ClickPesa au mtandao zinaweza kutumika na zitaonyeshwa kwenye prompt. Thibitisha malipo kwenye simu yako; usiweke PIN au OTP kwenye tovuti hii.</p>
              )}
              {(!isLivePayment || isAblinerLive) && (
                <fieldset className="payment-fieldset">
                  <legend>Chagua mtandao wa malipo</legend>
                  <div className="method-grid">
                    {paymentMethods.map((method) => (
                      <button key={method.value} type="button" className={`method-option ${form.payment_method === method.value ? "selected" : ""}`} onClick={() => setForm({ ...form, payment_method: method.value })}>
                        {method.logo ? <img className="method-logo" src={method.logo} alt="" /> : <span className={`method-icon ${method.color}`}>{method.short}</span>}
                        <span className="method-label">{method.label}</span><span className="radio-indicator" />
                      </button>
                    ))}
                  </div>
                </fieldset>
              )}
              {error && <p className="form-error" role="alert">{error}</p>}
              <button className="generate-button" type="submit" disabled={busy}>{busy ? "Inaandaa oda..." : `Endelea — TZS ${formatMoney(product.amount)}`}</button>
            </form>
            <button className="cart-secondary-button" type="button" onClick={addToCart}>Ongeza bidhaa nyingine kwenye kikapu</button>
            {cartCount > 0 && <a className="cart-link" href="/cart">Nenda kikapu ({cartCount})</a>}
            <p className="checkout-footnote">{isAblinerSelected ? "Malipo yatakusanywa kupitia Abliner kwenye akaunti ya jukwaa; mgao wa muuzaji utalipwa kwa mikono." : isLivePayment ? "Malipo yatakusanywa na ClickPesa kwenye akaunti ya jukwaa; mgao wa muuzaji utalipwa kwa mikono." : "Huu ni mfumo wa majaribio: hakuna pesa inayokatwa au kuhamishwa kwa muuzaji."}</p>
          </section>
        )}
        {orderId && order && !isCompleted && order.payment_status !== "FAILED" && (
          <section className="checkout-card">
            <div className="eyebrow"><span /> {order.demo ? "MALIPO YA MAJARIBIO" : order.payment_method === ABLINER_PAYMENT_METHOD ? "MALIPO YA ABLINER" : "MALIPO YA CLICKPESA"}</div>
            <h1>{order.demo ? "Thibitisha oda yako" : "Subiri ombi la malipo"}</h1>
            <div className="receipt-product"><span>{order.product_name || product?.name}</span><strong>TZS {formatMoney(order.amount)}</strong></div>
            <div className="payment-result pending">
              <strong>Haijalipwa bado</strong>
              {order.demo ? (
                <span>Bonyeza kitufe chini kuthibitisha demo. Hakuna pesa halisi itakayohamishwa.</span>
              ) : order.payment_method === ABLINER_PAYMENT_METHOD ? (
                <span>Ombi la malipo la Abliner limetumwa kwa namba uliyoingiza. Thibitisha kwenye simu yako; usiweke PIN au OTP kwenye tovuti hii.</span>
              ) : (
                <span>Ombi la malipo limetumwa kwa namba uliyoingiza. Ada za ClickPesa au mtandao zinaweza kutumika na zitaonyeshwa kwenye prompt. Thibitisha kwenye simu yako; usiweke PIN au OTP kwenye tovuti hii.</span>
              )}
              <span>Njia ya malipo: {paymentMethodLabel(order.payment_method || (isAblinerSelected ? ABLINER_PAYMENT_METHOD : selectedMethod?.label))}{order.bank_branch ? ` — ${order.bank_branch}` : ""}.</span>
            </div>
            {error && <p className="form-error" role="alert">{error}</p>}
            {order.demo && <button className="generate-button" type="button" disabled={busy} onClick={completeDemo}>{busy ? "Inathibitisha..." : "Thibitisha malipo ya majaribio"}</button>}
          </section>
        )}
        {orderId && order?.payment_status === "FAILED" && (
          <section className="checkout-card">
            <div className="eyebrow"><span /> MALIPO HAYAKUKAMILIKA</div>
            <h1>Malipo hayakupokelewa</h1>
            <p className="buyer-intro">Hakuna uthibitisho wa malipo uliopokelewa. Unaweza kuanzisha ombi jipya.</p>
            <button className="generate-button" type="button" onClick={retryPayment}>Jaribu tena</button>
          </section>
        )}
        {orderId && isCompleted && (
          <section className="checkout-card receipt-card">
            <img className="receipt-logo" src="/images/liparahisi-logo.svg" alt="LipaRahisi" />
            <div className="receipt-success" aria-hidden="true">✓</div>
            <p className="receipt-kicker">{order.demo ? "RISITI YA MAJARIBIO" : "RISITI YA MALIPO"}</p>
            <h1>Oda imekamilika</h1>
            <div className="receipt-product"><span>{order.product_name || product?.name}</span><strong>TZS {formatMoney(order.amount)}</strong></div>
            <dl className="receipt-details">
              <div><dt>Namba ya oda</dt><dd>{order.order_id}</dd></div>
              <div><dt>Mnunuzi</dt><dd>{order.buyer_name || form.buyer_name || "Mgeni"}</dd></div>
              <div><dt>Njia ya malipo</dt><dd>{paymentMethodLabel(order.payment_method)}{order.bank_branch ? ` — ${order.bank_branch}` : ""}</dd></div>
              <div><dt>Hali</dt><dd>{order.demo ? "Imekamilika (demo)" : "Malipo yamepokelewa"}</dd></div>
              <div><dt>Jumla ya malipo</dt><dd>TZS {formatMoney(order.amount)}</dd></div>
              <div><dt>Kamisheni ya jukwaa ({order.commission_percent}%)</dt><dd>TZS {formatMoney(order.platform_fee)}</dd></div>
              <div><dt>Mgao wa muuzaji baada ya kamisheni</dt><dd>TZS {formatMoney(order.seller_payout)}</dd></div>
              <div><dt>Tarehe</dt><dd>{order.completed_at || "—"}</dd></div>
            </dl>
            {order.demo && <p className="demo-receipt-note">Risiti hii inathibitisha oda ya majaribio pekee; si uthibitisho wa muamala wa pesa halisi.</p>}
            <button type="button" className="document-button" onClick={() => window.print()}>Chapisha / hifadhi kama PDF</button>
            <button type="button" className="document-button" onClick={() => downloadOrderReceipt().catch((downloadError) => setError(`Imeshindikana kutengeneza PDF: ${downloadError.message}`))}>Pakua risiti PDF</button>
            <a className="retry-link" href="/">Rudi mwanzo</a>
          </section>
        )}
      </div>
      <BrandFooter><span key="copyright">© 2026 LipaRahisi</span><span key="merchant">{product?.shop_name || "Malipo ya bidhaa"}</span></BrandFooter>
    </main>
  );
}

function CartPage({ checkoutId: initialCheckoutId = "" }) {
  const localize = useLocalize();
  const { language } = useLanguage();
  const { notify } = useNotification();
  const [items, setItems] = useState(loadCart());
  const [products, setProducts] = useState([]);
  const [paymentConfig, setPaymentConfig] = useState(null);
  const [checkoutId, setCheckoutId] = useState(initialCheckoutId);
  const [checkout, setCheckout] = useState(null);
  const previousCheckoutStatus = useRef(null);
  const [form, setForm] = useState({ buyer_name: "", buyer_email: "", buyer_phone: "", delivery_address: "", payment_method: "M-Pesa" });
  const [error, setError] = useFeedbackState("error");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all(items.map((item) => api(`/products/${item.product_id}`).then((product) => ({ ...item, ...product })).catch(() => null)))
      .then((rows) => setProducts(rows.filter(Boolean)));
  }, []);

  useEffect(() => {
    let active = true;
    api("/config/integrations")
      .then((config) => {
        if (active) {
          setPaymentConfig(config);
          if (
            config.abliner_live_enabled === true
            && config.demo_mode !== true
            && config.live_payments_enabled !== true
          ) {
            setForm((current) => current.payment_method === "M-Pesa"
              ? { ...current, payment_method: ABLINER_PAYMENT_METHOD }
              : current);
          }
        }
      })
      .catch((requestError) => {
        if (active) setError(requestError.message);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!checkoutId) return;
    let active = true;
    let timer;
    async function refreshCheckoutStatus() {
      try {
        const status = await api(`/checkouts/${encodeURIComponent(checkoutId)}`);
        if (!active) return;
        if (previousCheckoutStatus.current === "PENDING" && status.payment_status === "COMPLETED") {
          notify("Malipo ya oda yamekamilika.", "success");
        }
        previousCheckoutStatus.current = status.payment_status;
        setCheckout(status);
        if (status.payment_status === "PENDING") {
          timer = window.setTimeout(refreshCheckoutStatus, 5000);
        }
      } catch (requestError) {
        if (active) {
          setError(requestError.message);
          timer = window.setTimeout(refreshCheckoutStatus, 5000);
        }
      }
    }
    refreshCheckoutStatus();
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [checkoutId, notify]);

  function updateQuantity(productId, quantity) {
    const next = items.map((item) => item.product_id === productId ? { ...item, quantity: Math.max(1, quantity) } : item).filter((item) => item.quantity > 0);
    setItems(next);
    saveCart(next);
  }

  function removeItem(productId) {
    const next = items.filter((item) => item.product_id !== productId);
    setItems(next);
    saveCart(next);
  }

  async function submitCart(event) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const result = await api("/cart/checkout", {
        method: "POST",
        body: JSON.stringify({
          request_key: newRequestKey(),
          buyer_name: form.buyer_name,
          buyer_email: form.buyer_email,
          buyer_phone: form.buyer_phone,
          delivery_address: form.delivery_address,
          payment_method: form.payment_method,
          items: items.map((item) => ({
            product_id: item.product_id,
            quantity: item.quantity,
          })),
        }),
      });
      setCheckoutId(result.checkout_id);
      window.history.replaceState({}, "", `/cart/${result.checkout_id}`);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function completeCart() {
    setError("");
    setBusy(true);
    try {
      const result = await api(`/checkouts/${encodeURIComponent(checkoutId)}/demo-complete`, { method: "POST" });
      setCheckout(result);
      if (result.payment_status === "COMPLETED") notify("Malipo ya oda yamekamilika.", "success");
      saveCart([]);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  function retryCartPayment() {
    setCheckoutId("");
    setCheckout(null);
    setError("");
    previousCheckoutStatus.current = null;
    window.history.replaceState({}, "", "/cart");
  }

  async function downloadCartReceipt() {
    if (!checkout || checkout.payment_status !== "COMPLETED") return;
    await downloadReceiptPdf({
      checkoutId: checkout.checkout_id,
      buyerName: checkout.buyer_name || form.buyer_name || "Mgeni",
      buyerEmail: checkout.buyer_email || form.buyer_email,
      buyerPhone: checkout.buyer_phone || form.buyer_phone,
      paymentMethod: paymentMethodLabel(checkout.payment_method),
      bankBranch: checkout.bank_branch,
      totalAmount: checkout.total_amount,
      platformFee: checkout.platform_fee,
      sellerPayout: checkout.splits.reduce((sum, split) => sum + Number(split.seller_payout || 0), 0),
      commissionPercent: checkout.commission_percent,
      completedAt: checkout.completed_at,
      splits: checkout.splits.map((split) => ({
        shopName: split.shop_name,
        amount: split.amount,
        platformFee: split.platform_fee,
        sellerPayout: split.seller_payout,
        items: split.items,
      })),
    }, language);
  }

  const total = products.reduce((sum, product) => sum + product.amount * (items.find((item) => item.product_id === product.id)?.quantity || 0), 0);
  const isLivePayment = paymentConfig?.live_payments_enabled === true;
  const isAblinerLive = paymentConfig?.abliner_live_enabled === true;
  const isAblinerSelected = form.payment_method === ABLINER_PAYMENT_METHOD;
  const paymentMethods = paymentOptionsFor(paymentConfig);

  return localize(
    <main className="page-shell checkout-shell">
      <Header label="Kikapu cha malipo" />
      <div className="checkout-layout cart-layout">
        <section className="checkout-card">
          {!checkoutId && (
            <>
              <div className="eyebrow"><span /> KIKAPU CHA BIDHAA</div>
              <h1>Chagua bili yako</h1>
              {products.length === 0 && <p className="buyer-intro">Kikapu kiko tupu. Scan QR ya bidhaa ili uongeze bidhaa.</p>}
              {products.map((product) => (
                <div className="cart-row" key={product.id}>
                  <div><strong>{product.name}</strong><span>Wauzaji: {product.shop_name}</span><small>TZS {formatMoney(product.amount)}</small></div>
                  <input aria-label={`Idadi ya ${product.name}`} className="cart-quantity" type="number" min="1" max="99" value={items.find((item) => item.product_id === product.id)?.quantity || 1} onChange={(event) => updateQuantity(product.id, Number(event.target.value) || 1)} />
                  <button type="button" onClick={() => removeItem(product.id)}>Ondoa</button>
                </div>
              ))}
              {products.length > 0 && (
                <form className="buyer-form" onSubmit={submitCart}>
                  <label className="field-label" htmlFor="cart-buyer-name">Jina la mnunuzi</label>
                  <input id="cart-buyer-name" className="text-input" autoComplete="name" required minLength={2} value={form.buyer_name} onChange={(event) => setForm({ ...form, buyer_name: event.target.value })} />
                  <label className="field-label" htmlFor="cart-buyer-email">Barua pepe</label>
                  <input id="cart-buyer-email" className="text-input" type="email" autoComplete="email" required value={form.buyer_email} onChange={(event) => setForm({ ...form, buyer_email: event.target.value })} />
                  <label className="field-label" htmlFor="cart-buyer-phone">Namba ya simu</label>
                  <input id="cart-buyer-phone" className="text-input" type="tel" inputMode="tel" required value={form.buyer_phone} onChange={(event) => setForm({ ...form, buyer_phone: event.target.value })} />
                  <label className="field-label" htmlFor="cart-buyer-address">Anuani ya delivery (hiari)</label>
                  <input id="cart-buyer-address" className="text-input" maxLength={500} autoComplete="street-address" value={form.delivery_address} onChange={(event) => setForm({ ...form, delivery_address: event.target.value })} />
                  {isAblinerSelected && (
                    <p className="field-hint">Abliner itatuma ombi moja la malipo kwa jumla ya kikapu. Thibitisha kwenye simu yako; usiweke PIN au OTP hapa.</p>
                  )}
                  {isLivePayment && !isAblinerSelected && (
                    <p className="field-hint">ClickPesa itatuma ombi moja la USSD kwa jumla ya kikapu. Ada za ClickPesa au mtandao zinaweza kutumika na zitaonyeshwa kwenye prompt. Thibitisha kwenye simu yako; usiweke PIN au OTP hapa.</p>
                  )}
                  {(!isLivePayment || isAblinerLive) && (
                    <fieldset className="payment-fieldset">
                      <legend>Chagua mtandao wa malipo</legend>
                      <div className="method-grid">
                        {paymentMethods.map((method) => (
                          <button key={method.value} type="button" className={`method-option ${form.payment_method === method.value ? "selected" : ""}`} onClick={() => setForm({ ...form, payment_method: method.value })}>
                            {method.logo ? <img className="method-logo" src={method.logo} alt="" /> : <span className={`method-icon ${method.color}`}>{method.short}</span>}
                            <span className="method-label">{method.label}</span><span className="radio-indicator" />
                          </button>
                        ))}
                      </div>
                    </fieldset>
                  )}
                  {error && <p className="form-error" role="alert">{error}</p>}
                  <button className="generate-button" type="submit" disabled={busy}>{busy ? "Inaandaa bili..." : `Lipa TZS ${formatMoney(total)}`}</button>
                </form>
              )}
              <p className="checkout-footnote">{isAblinerSelected ? "Malipo yatakusanywa kupitia Abliner kwenye akaunti ya jukwaa; mgao wa wauzaji utalipwa kwa mikono." : isLivePayment ? "Malipo yatakusanywa na ClickPesa kwenye akaunti ya jukwaa; mgao wa wauzaji utalipwa kwa mikono." : "Kikapu hiki ni majaribio. Malipo halisi hayajaunganishwa."}</p>
            </>
          )}
          {checkoutId && checkout && (
            <>
              <div className="eyebrow"><span /> {checkout.demo ? "ODA YA MAJARIBIO" : checkout.payment_method === ABLINER_PAYMENT_METHOD ? "MALIPO YA ABLINER" : "MALIPO YA CLICKPESA"}</div>
              {checkout.payment_status === "COMPLETED" && <img className="receipt-logo" src="/images/liparahisi-logo.svg" alt="LipaRahisi" />}
              <h1>{checkout.payment_status === "COMPLETED" ? "Malipo yamekamilika" : checkout.demo ? "Gawanya kwa wauzaji" : "Subiri ombi la malipo"}</h1>
              {checkout.splits.map((split) => (
                <div className="cart-split" key={split.order_id}>
                  <strong>{split.shop_name}</strong>
                  <span>{split.items.map((item) => `${item.quantity} x ${item.product_name} @ TZS ${formatMoney(item.unit_amount)} = TZS ${formatMoney(item.quantity * item.unit_amount)}`).join(", ")}</span>
                  <b>TZS {formatMoney(split.amount)}</b>
                  <small>Kamisheni ({checkout.commission_percent}%): TZS {formatMoney(split.platform_fee)}</small>
                  <small>Mgao wa muuzaji baada ya kamisheni: TZS {formatMoney(split.seller_payout)}</small>
                </div>
              ))}
              <div className="checkout-amount">Jumla ya mteja: TZS {formatMoney(checkout.total_amount)}</div>
              <p className="field-hint">Kamisheni inakatwa kwenye mgao wa kila muuzaji; haiongezwi kwenye bili ya mnunuzi.</p>
              {checkout.payment_status === "PENDING" && !checkout.demo && (
                <p className="payment-result pending">
                  {checkout.payment_method === ABLINER_PAYMENT_METHOD
                    ? "Ombi moja la Abliner limetumwa kwa jumla ya kikapu. Thibitisha kwenye simu yako; PIN/OTP haihitajiki kwenye tovuti."
                    : "Ombi moja la malipo limetumwa kwa namba uliyoweka. Ada za ClickPesa au mtandao zinaweza kutumika na zitaonyeshwa kwenye prompt. Thibitisha kwenye simu yako; PIN/OTP haihitajiki kwenye tovuti."}
                </p>
              )}
              {checkout.payment_status === "PENDING" && checkout.demo && (
                <button className="generate-button" type="button" disabled={busy} onClick={completeCart}>{busy ? "Inathibitisha..." : "Thibitisha demo kwa wauzaji wote"}</button>
              )}
              {checkout.payment_status === "COMPLETED" && <p className="success-message">{checkout.demo ? "Oda zote za wauzaji zimekamilika kama majaribio." : "Malipo yamepokelewa. Mgao wa wauzaji utalipwa kwa mikono."}</p>}
              {checkout.payment_status === "FAILED" && (
                <div className="payment-result pending">
                  <strong>Malipo hayakupokelewa</strong>
                  <span>Hakuna uthibitisho wa malipo uliopokelewa. Unaweza kuanzisha ombi jipya.</span>
                  <button className="generate-button" type="button" onClick={retryCartPayment}>Jaribu tena</button>
                </div>
              )}
              {checkout.payment_status === "EXPIRED" && <p className="form-error">Oda imekwisha muda; anzisha kikapu kipya.</p>}
              {error && <p className="form-error" role="alert">{error}</p>}
              {checkout.payment_status === "COMPLETED" && (
                <>
                  <dl className="receipt-details">
                    <div><dt>Mnunuzi</dt><dd>{checkout.buyer_name || form.buyer_name || "Mgeni"}</dd></div>
                    <div><dt>Njia ya malipo</dt><dd>{paymentMethodLabel(checkout.payment_method)}{checkout.bank_branch ? ` — ${checkout.bank_branch}` : ""}</dd></div>
                    <div><dt>Kamisheni jumla ({checkout.commission_percent}%)</dt><dd>TZS {formatMoney(checkout.platform_fee)}</dd></div>
                    <div><dt>Mgao wa wauzaji baada ya kamisheni</dt><dd>TZS {formatMoney(checkout.splits.reduce((sum, split) => sum + Number(split.seller_payout || 0), 0))}</dd></div>
                    <div><dt>Tarehe</dt><dd>{checkout.completed_at || "—"}</dd></div>
                  </dl>
                  {checkout.demo && <p className="demo-receipt-note">Risiti hii inathibitisha oda ya majaribio pekee; si uthibitisho wa muamala wa pesa halisi.</p>}
                  <button type="button" className="document-button" onClick={() => window.print()}>Chapisha / hifadhi risiti kama PDF</button>
                  <button type="button" className="document-button" onClick={() => downloadCartReceipt().catch((downloadError) => setError(`Imeshindikana kutengeneza PDF: ${downloadError.message}`))}>Pakua risiti PDF</button>
                </>
              )}
              <a className="retry-link" href="/">Rudi mwanzo</a>
            </>
          )}
        </section>
      </div>
      <BrandFooter><span key="copyright">© 2026 LipaRahisi</span><span key="qr-info">QR ya bidhaa ni reusable; oda ya malipo ina muda wa saa 24</span></BrandFooter>
    </main>
  );
}

export default function App() {
  const localize = useLocalize();
  const checkoutMatch = window.location.pathname.match(/^\/(?:pay|p)\/([a-f0-9]{32})\/?$/i);
  const cartMatch = window.location.pathname.match(/^\/cart(?:\/([A-Z0-9]+))?\/?$/i);
  const storefrontMatch = window.location.pathname.match(/^\/store\/([^/]+)\/?$/);
  const forgotMatch = window.location.pathname === "/forgot-password";
  const resetMatch = window.location.pathname === "/reset-password";
  const verifyMatch = window.location.pathname === "/verify-email";
  const [auth, setAuth] = useState(null);
  const [authState, setAuthState] = useState(null);
  const [authError, setAuthError] = useFeedbackState("error");
  const [showAuth, setShowAuth] = useState(false);

  useEffect(() => {
    if (checkoutMatch || cartMatch || storefrontMatch || forgotMatch || resetMatch || verifyMatch) return;
    api("/auth/status")
      .then(setAuthState)
      .catch((requestError) => setAuthError(requestError.message));
  }, []);

  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
      setAuth(null);
      setAuthState((state) => state ? { ...state, user: null } : state);
    } catch (requestError) {
      setAuthError(requestError.message);
    }
  }

  if (checkoutMatch) return <CheckoutPage productId={checkoutMatch[1]} />;
  if (cartMatch) return <CartPage checkoutId={cartMatch[1] || ""} />;
  if (storefrontMatch) return <StorefrontPage storeSlug={decodeURIComponent(storefrontMatch[1])} />;
  if (forgotMatch) return <ForgotPasswordPage />;
  if (resetMatch) return <ResetPasswordPage />;
  if (verifyMatch) return <VerifyEmailPage />;
  if (authError) {
    return localize(<main className="page-shell auth-shell"><Header label="Marketplace" /><p className="form-error">{authError}</p></main>);
  }
  if (!authState) return localize(<main className="page-shell auth-shell"><Header label="Marketplace" /><p className="buyer-intro">Inapakia mfumo...</p></main>);
  if (auth) return auth.role === "BUYER"
    ? <BuyerDashboard user={auth} onLogout={logout} />
    : <MarketplaceDashboard user={auth} onLogout={logout} />;
  if (authState.user) return authState.user.role === "BUYER"
    ? <BuyerDashboard user={authState.user} onLogout={logout} />
    : <MarketplaceDashboard user={authState.user} onLogout={logout} />;
  if (!authState.setup_required && !showAuth) return <PublicMarketplace onLogin={() => setShowAuth(true)} />;
  return <AuthScreen
    setupRequired={authState.setup_required}
    setupAvailable={authState.setup_available}
    onAuth={setAuth}
    onBrowse={() => setShowAuth(false)}
  />;
}
