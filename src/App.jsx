import React, { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import JsBarcode from "jsbarcode";
import { backgroundThemes, translate, translateKey, useLanguage, useLocalize } from "./i18n.jsx";
import { formatDate, formatNumber } from "./translations.js";
import { useFeedbackState, useNotification } from "./notifications.jsx";
import { api, validateDocument, validateProductImage } from "./api.js";

const API = "/api";
const ABLINER_PAYMENT_METHOD = "Abliner Mobile Money";
const productImageTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const maxProductImageBytes = 5 * 1024 * 1024;
const maxProductGalleryImages = 8;
const productVideoTypes = new Set(["video/mp4", "video/webm"]);
const maxProductVideoBytes = 20 * 1024 * 1024;
const maxProductVideos = 3;
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

function formatMoney(amount, preferredLanguage) {
  let language = "sw";
  try {
    language = preferredLanguage || window.localStorage.getItem("liparahisi_language") || "sw";
  } catch {
    language = preferredLanguage || "sw";
  }
  return formatNumber(amount, language);
}

async function downloadReceiptPdf(receipt, language) {
  const t = (text) => translate(text, language);
  const { jsPDF } = await import("jspdf");
  const pdf = new jsPDF({ unit: "mm", format: "a4" });
  const margin = 16;
  const pageWidth = pdf.internal.pageSize.getWidth();
  const pageHeight = pdf.internal.pageSize.getHeight();
  const isSandbox = receipt.sandbox === true;
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
  writeLine(
    t(isSandbox ? "RISITI YA MALIPO - PESA PAL SANDBOX" : "RISITI YA MALIPO - DEMO"),
    { size: 11, bold: true, gap: 4 },
  );
  writeLine(`${t("Oda:")} ${receipt.checkoutId}`, { bold: true });
  writeLine(`${t("Mnunuzi:")} ${receipt.buyerName}`);
  if (receipt.buyerEmail) writeLine(`${t("Barua pepe:")} ${receipt.buyerEmail}`);
  if (receipt.buyerPhone) writeLine(`${t("Simu:")} ${receipt.buyerPhone}`);
  writeLine(`${t("Njia ya malipo:")} ${receipt.paymentMethod}${receipt.bankBranch ? ` - ${receipt.bankBranch}` : ""}`);
  if (receipt.deliveryAddress) writeLine(`${t("Anuani ya delivery:")} ${receipt.deliveryAddress}`);
  writeLine(
    t(isSandbox ? "Hali: Imehakikiwa kwenye sandbox" : "Hali: Imekamilika (DEMO)"),
    { bold: true, gap: 4 },
  );

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
  writeLine(`${t("Tarehe:")} ${formatDate(receipt.completedAt, language)}`);
  writeLine(
    t("Risiti hii ni ya majaribio; hakuna pesa halisi iliyohamishwa."),
    { size: 9, gap: 0 },
  );

  const filename = String(receipt.checkoutId || "oda").replace(/[^a-z0-9-]/gi, "");
  pdf.save(`risiti-${filename || "oda"}.pdf`);
}

function newRequestKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID().replace(/-/g, "");
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
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

function validateProductVideo(file) {
  if (!productVideoTypes.has(file.type)) {
    throw new Error(translate("Tumia video ya MP4 au WebM."));
  }
  if (file.size > maxProductVideoBytes) {
    throw new Error(translate("Video imezidi ukubwa wa MB 20."));
  }
}

async function uploadProductVideo(productId, file) {
  validateProductVideo(file);
  const formData = new FormData();
  formData.append("video", file);
  return api(`/products/${encodeURIComponent(productId)}/videos`, {
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
  const { language, setLanguage, backgroundColor, setBackgroundColor } = useLanguage();
  const localize = useLocalize();
  const { notify } = useNotification();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsRef = useRef(null);

  useEffect(() => {
    if (!settingsOpen) return undefined;
    function closeOnOutsideClick(event) {
      if (settingsRef.current && !settingsRef.current.contains(event.target)) setSettingsOpen(false);
    }
    function closeOnEscape(event) {
      if (event.key === "Escape") setSettingsOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [settingsOpen]);

  function changeLanguage(nextLanguage) {
    setLanguage(nextLanguage);
    if (!user) return;
    api("/auth/language", {
      method: "PUT",
      body: JSON.stringify({ language: nextLanguage }),
    }).catch((requestError) => {
      notify(requestError.message, "error");
    });
  }
  function openFullSettings() {
    setSettingsOpen(false);
    if (user) {
      window.location.hash = "settings";
      window.dispatchEvent(new CustomEvent("liparahisi-open-settings"));
    } else {
      document.querySelector(".dashboard-content")?.scrollIntoView({ behavior: "smooth" });
    }
  }
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
        {user?.profile_image_url && (
          <img className="header-avatar" src={user.profile_image_url} alt="" aria-hidden="true" key="avatar" />
        )}
        <span className="prototype-label" key="label"><span className="status-dot" key="status-dot" />{label}</span>
        <div className="settings-menu" ref={settingsRef} key="settings-menu">
          <button
            className="settings-menu-toggle"
            type="button"
            aria-label={translateKey("settings.title", language)}
            aria-haspopup="menu"
            aria-expanded={settingsOpen}
            onClick={() => setSettingsOpen((open) => !open)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z" />
              <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-1.42 1.42-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56v.08h-2v-.08A1.7 1.7 0 0 0 12.38 18a1.7 1.7 0 0 0-1.88.34l-.06.06-1.42-1.42.06-.06A1.7 1.7 0 0 0 9.42 15a1.7 1.7 0 0 0-1.56-1.03h-.08v-2h.08A1.7 1.7 0 0 0 9.42 10a1.7 1.7 0 0 0-.34-1.88l-.06-.06 1.42-1.42.06.06A1.7 1.7 0 0 0 12.38 7a1.7 1.7 0 0 0 1.03-1.56v-.08h2v.08A1.7 1.7 0 0 0 16.44 7a1.7 1.7 0 0 0 1.88-.34l.06-.06 1.42 1.42-.06.06A1.7 1.7 0 0 0 19.4 10a1.7 1.7 0 0 0 1.56 1.03h.08v2h-.08A1.7 1.7 0 0 0 19.4 15Z" />
            </svg>
          </button>
          {settingsOpen && (
            <div className="settings-menu-popover" role="menu" aria-label={translateKey("settings.menu", language)}>
              <div className="settings-menu-heading">
                <strong>{translateKey("settings.title", language)}</strong>
              </div>
              <label className="settings-menu-language">
                <span>{translateKey("language.label", language)}</span>
                <select
                  aria-label={translateKey("language.label", language)}
                  value={language}
                  onChange={(event) => changeLanguage(event.target.value)}
                >
                  <option value="sw">🇹🇿 Kiswahili</option>
                  <option value="en">🇬🇧 English</option>
                  <option value="fr">🇫🇷 Français</option>
                </select>
              </label>
              <div className="settings-menu-colors" role="group" aria-label={translateKey("settings.background.title", language)}>
                <span>{translateKey("settings.background.title", language)}</span>
                <div>
                  {backgroundThemes.map((theme) => (
                    <button
                      key={theme.color}
                      type="button"
                      className={backgroundColor === theme.color ? "selected" : ""}
                      style={{ backgroundColor: theme.color }}
                      aria-label={translateKey("settings.background.select", language, { color: translateKey(theme.labelKey, language) })}
                      aria-pressed={backgroundColor === theme.color}
                      onClick={() => setBackgroundColor(theme.color)}
                    />
                  ))}
                </div>
              </div>
              {user && ["BUYER", "SELLER", "ADMIN"].includes(user.role) && (
                <button className="settings-menu-full" type="button" role="menuitem" onClick={openFullSettings}>
                  {translateKey("settings.open", language)}
                </button>
              )}
            </div>
          )}
        </div>
        {user && (
          <button
            className="logout-button"
            type="button"
            onClick={onLogout}
            aria-label={`${translate("Toka kwenye akaunti ya", language)} ${user.full_name}`}
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
    totp_code: "",
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
          body: JSON.stringify({
            email: form.email,
            password: form.password,
            totp_code: form.totp_code,
          }),
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
          {mode === "login" && (
            <>
              <label className="field-label" htmlFor="auth-totp">Nambari ya authenticator au recovery code (ikiwa 2FA imewashwa)</label>
              <input id="auth-totp" className="text-input" name="totp_code" inputMode="text" maxLength={32} autoComplete="one-time-code" value={form.totp_code} onChange={change} />
              <p className="field-hint">Ikiwa 2FA imewashwa, ingiza nambari ya tarakimu 6 kutoka kwenye authenticator app au recovery code iliyohifadhiwa.</p>
            </>
          )}
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
  onUploadVideos,
  onDeleteImage,
  onDeleteGalleryImage,
  onDeleteVideo,
  onAddVariant,
  onUpdateVariantStock,
  canArchive,
}) {
  const localize = useLocalize();
  const { language } = useLanguage();
  const [qr, setQr] = useState("");
  const [variantStocks, setVariantStocks] = useState({});
  const [newVariantLabel, setNewVariantLabel] = useState("");
  const [newVariantStock, setNewVariantStock] = useState(String(product.stock_quantity ?? 0));
  const barcodeRef = useRef(null);
  useEffect(() => {
    setVariantStocks(Object.fromEntries(
      (product.variants || []).map((variant) => [variant.id, String(variant.stock_quantity)]),
    ));
    setNewVariantStock(product.variants?.length ? "0" : String(product.stock_quantity ?? 0));
    setNewVariantLabel("");
  }, [product.id, product.variants]);

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

  async function submitNewVariant(event) {
    event.preventDefault();
    const added = await onAddVariant(product.id, newVariantLabel, Number(newVariantStock));
    if (added) {
      setNewVariantLabel("");
      setNewVariantStock("0");
    }
  }

  return localize(
    <article className={`product-tile ${product.is_active ? "" : "product-inactive"}`}>
      <ProductGallery
        product={product}
        className="product-card-gallery"
        editable={canArchive}
        onDeleteImage={onDeleteGalleryImage}
      />
      {product.videos?.length > 0 && (
        <div className="seller-product-videos">
          {product.videos.map((video) => (
            <div className="seller-product-video" key={video.id}>
              <video controls preload="metadata" playsInline src={video.url} />
              {canArchive && (
                <button
                  className="table-action"
                  type="button"
                  onClick={() => onDeleteVideo(product.id, video.id)}
                >
                  {translate("Ondoa video", language)}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {qr ? <img className="product-qr" src={qr} alt={`${translate("QR ya", language)} ${product.name}`} /> : <div className="product-qr qr-placeholder" />}
      <div className="product-tile-details">
        <strong>{product.name}</strong>
        {product.category && <small>{product.category}</small>}
        {product.description && <small>{product.description}</small>}
        <span>TZS {formatMoney(product.amount)}</span>
        <small>Stock: {product.stock_quantity ?? 0}</small>
        {(product.variants?.length > 0 || canArchive) && (
          <div className="variant-stock-list">
            <strong>Stock kwa variant</strong>
            {!product.variants?.length && (
              <small>Stock ya kawaida itakuwa stock ya variant mpya.</small>
            )}
            {(product.variants || []).map((variant) => (
              <div className="variant-stock-row" key={variant.id}>
                <span>{variant.label}: {variant.stock_quantity}</span>
                {canArchive && (
                  <>
                    <input
                      className="text-input"
                      type="number"
                      min="0"
                      max="999999999"
                      aria-label={`${translate("Stock kwa variant", language)} ${variant.label}`}
                      value={variantStocks[variant.id] ?? String(variant.stock_quantity)}
                      onChange={(event) => setVariantStocks((current) => ({
                        ...current,
                        [variant.id]: event.target.value,
                      }))}
                    />
                    <button
                      className="table-action"
                      type="button"
                      disabled={!Number.isInteger(Number(variantStocks[variant.id] ?? variant.stock_quantity))}
                      onClick={() => onUpdateVariantStock(
                        product.id,
                        variant.id,
                        Number(variantStocks[variant.id] ?? variant.stock_quantity),
                      )}
                    >
                      Hifadhi stock
                    </button>
                  </>
                )}
              </div>
            ))}
            {canArchive && (
              <form className="variant-stock-row" onSubmit={submitNewVariant}>
                <input
                  className="text-input"
                  aria-label={translate("Variant", language)}
                  placeholder={translate("Mfano: Rangi nyeusi / Size M", language)}
                  maxLength={80}
                  required
                  value={newVariantLabel}
                  onChange={(event) => setNewVariantLabel(event.target.value)}
                />
                <input
                  className="text-input"
                  type="number"
                  min="0"
                  max="999999999"
                  aria-label={translate("Stock kwa variant", language)}
                  required
                  value={newVariantStock}
                  onChange={(event) => setNewVariantStock(event.target.value)}
                />
                <button className="table-action" type="submit">Ongeza variant</button>
              </form>
            )}
          </div>
        )}
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
                aria-label={`${translate("Pakia picha ya", language)} ${product.name}`}
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
          {canArchive && (
            <label className={`table-action ${product.videos?.length >= maxProductVideos ? "is-disabled" : ""}`}>
              {translate("Pakia video", language)} ({product.videos?.length || 0}/{maxProductVideos})
              <input
                type="file"
                accept="video/mp4,video/webm"
                aria-label={`${translate("Pakia video", language)} ${product.name}`}
                multiple
                hidden
                disabled={(product.videos?.length || 0) >= maxProductVideos}
                onChange={(event) => {
                  const files = Array.from(event.target.files || []);
                  if (files.length) onUploadVideos(product.id, files);
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
      <svg ref={barcodeRef} className="product-barcode" aria-label={`${translate("Barcode ya", language)} ${product.name}`} />
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

function DashboardTrendChart({ title, rows = [] }) {
  const localize = useLocalize();
  const values = rows.map((row) => Number(row.sales || 0));
  const maxValue = Math.max(1, ...values);
  return localize(
    <section className="dashboard-panel">
      <h2>{title}</h2>
      {rows.length ? (
        <div className="dashboard-chart" role="img" aria-label={title}>
          {rows.map((row) => {
            const value = Number(row.sales || 0);
            return (
              <div className="dashboard-chart-column" key={row.day}>
                <strong>TZS {formatMoney(value)}</strong>
                <div className="dashboard-chart-track">
                  <span style={{ height: `${Math.max(4, value / maxValue * 100)}%` }} />
                </div>
                <small>{row.day}</small>
              </div>
            );
          })}
          {detailProduct && (
            <div className="product-detail-backdrop" role="presentation" onClick={() => setDetailProduct(null)}>
              <section
                className="product-detail-modal"
                role="dialog"
                aria-modal="true"
                aria-label={detailProduct.name}
                onClick={(event) => event.stopPropagation()}
              >
                <button className="product-detail-close" type="button" aria-label={translate("Funga", language)} onClick={() => setDetailProduct(null)}>×</button>
                <ProductGallery product={detailProduct} className="product-detail-gallery" />
                <div className="product-detail-copy">
                  <span>{detailProduct.category || translate("Bidhaa", language)}</span>
                  <h2>{detailProduct.name}</h2>
                  <p>{detailProduct.description || translate("Hakuna maelezo ya bidhaa.", language)}</p>
                  <strong>TZS {formatMoney(detailProduct.amount, language)}</strong>
                  <small>{detailProduct.shop_name || translate("Duka", language)}</small>
                  <div className="market-product-actions">
                    {showSave && (
                      <button type="button" className="table-action" onClick={() => onSave(detailProduct)}>
                        {translate("Hifadhi", language)}
                      </button>
                    )}
                    <a className="generate-button" href={`/pay/${detailProduct.id}`}>
                      {translate("Nunua sasa", language)}
                    </a>
                  </div>
                </div>
              </section>
            </div>
          )}
        </div>
      ) : <p className="empty-state">Hakuna mauzo katika kipindi hiki.</p>}
    </section>
  );
}

function DashboardStatusList({ title, rows = [] }) {
  const localize = useLocalize();
  return localize(
    <section className="dashboard-panel">
      <h2>{title}</h2>
      {rows.length ? (
        <div className="dashboard-status-list">
          {rows.map((row) => (
            <div className="dashboard-status-row" key={row.status}>
              <span>{row.status.replace(/_/g, " ")}</span>
              <strong>{row.count}</strong>
            </div>
          ))}
        </div>
      ) : <p className="empty-state">Hakuna taarifa kwa sasa.</p>}
    </section>
  );
}

function DashboardActivityFeed({ rows = [] }) {
  const localize = useLocalize();
  return localize(
    <section className="dashboard-panel">
      <h2>Shughuli za hivi karibuni</h2>
      {rows.length ? (
        <div className="dashboard-activity-list">
          {rows.map((row, index) => (
            <article key={`${row.created_at}-${row.action}-${index}`}>
              <strong>{row.action.replace(/_/g, " ")}</strong>
              <span>{row.actor_email || "Mfumo"} · {row.target_type || "jukwaa"}</span>
              <small>{row.created_at}</small>
            </article>
          ))}
        </div>
      ) : <p className="empty-state">Hakuna shughuli zilizorekodiwa bado.</p>}
    </section>
  );
}

function AccountSecurity() {
  const { language } = useLanguage();
  const localize = useLocalize();
  const [status, setStatus] = useState({ enabled: false, enrollment_available: false });
  const [sessions, setSessions] = useState([]);
  const [setupUri, setSetupUri] = useState("");
  const [setupSecret, setSetupSecret] = useState("");
  const [qr, setQr] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState([]);
  const [notificationPreferences, setNotificationPreferences] = useState({
    email_enabled: true,
    sms_enabled: false,
    web_push_enabled: true,
    mobile_push_enabled: false,
    whatsapp_enabled: false,
    security_alerts_enabled: true,
    marketing_enabled: false,
  });
  const [notificationHistory, setNotificationHistory] = useState([]);
  const [error, setError] = useFeedbackState("error");
  const [message, setMessage] = useFeedbackState("success");

  async function refresh() {
    const [mfaStatus, sessionRows, preferences, history] = await Promise.all([
      api("/auth/mfa/status"),
      api("/auth/sessions"),
      api("/notifications/preferences"),
      api("/notifications/history"),
    ]);
    setStatus(mfaStatus);
    setSessions(sessionRows);
    setNotificationPreferences(preferences);
    setNotificationHistory(history);
  }

  useEffect(() => {
    refresh().catch((requestError) => setError(requestError.message));
  }, []);

  useEffect(() => {
    let active = true;
    if (!setupUri) {
      setQr("");
      return () => { active = false; };
    }
    QRCode.toDataURL(setupUri, { width: 220, margin: 1 })
      .then((image) => { if (active) setQr(image); })
      .catch((requestError) => {
        if (active) setError(`Imeshindikana kutengeneza QR ya 2FA: ${requestError.message}`);
      });
    return () => { active = false; };
  }, [setupUri, setError]);

  async function startSetup() {
    try {
      const setup = await api("/auth/mfa/setup", { method: "POST" });
      setSetupUri(setup.otpauth_uri);
      setSetupSecret(setup.secret);
      setCode("");
      setRecoveryCodes([]);
      setError("");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function enable(event) {
    event.preventDefault();
    try {
      const result = await api("/auth/mfa/enable", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      setSetupUri("");
      setSetupSecret("");
      setCode("");
      setRecoveryCodes(result.recovery_codes);
      setMessage("2FA imewashwa. Hifadhi recovery codes hizi mahali salama.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function disable(event) {
    event.preventDefault();
    try {
      await api("/auth/mfa/disable", {
        method: "POST",
        body: JSON.stringify({ password, code }),
      });
      setCode("");
      setPassword("");
      setRecoveryCodes([]);
      setMessage("2FA imezimwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function revokeOtherDevices() {
    try {
      const result = await api("/auth/sessions/revoke-others", { method: "POST" });
      setMessage(`Vikao ${result.revoked_count} vya vifaa vingine vimefungwa.`);
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function saveNotificationPreferences(event) {
    event.preventDefault();
    try {
      await api("/notifications/preferences", {
        method: "PUT",
        body: JSON.stringify(notificationPreferences),
      });
      setMessage("Mapendeleo ya arifa yamehifadhiwa.");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  return localize(
    <div className="dashboard-content">
      {error && <p className="form-error dashboard-alert" role="alert">{error}</p>}
      {message && <p className="success-message dashboard-alert" role="status">{message}</p>}
      <section className="form-card settings-form">
        <h2>Uthibitishaji wa hatua mbili (2FA)</h2>
        <p className="field-hint">Hali: {status.enabled ? "Umewashwa" : "Umezimwa"}. Tumia authenticator app yenye TOTP.</p>
        {!status.enabled && !setupUri && <button className="generate-button" type="button" disabled={!status.enrollment_available} onClick={startSetup}>Sanidi 2FA</button>}
        {!status.enrollment_available && <p className="field-hint">MFA_ENCRYPTION_KEY haijawekwa kwenye server; 2FA haiwezi kusanidiwa bado.</p>}
        {setupUri && (
          <form className="buyer-form" onSubmit={enable}>
            {qr && <img src={qr} alt="QR code ya 2FA" width="220" height="220" />}
            <p className="field-hint">Siri ya kuingiza kwa mkono: <code>{setupSecret}</code></p>
            <label className="field-label" htmlFor="mfa-setup-code">Weka nambari ya tarakimu 6 kutoka kwenye app</label>
            <input id="mfa-setup-code" className="text-input" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} required value={code} onChange={(event) => setCode(event.target.value)} />
            <button className="generate-button" type="submit">Thibitisha na washa 2FA</button>
          </form>
        )}
        {status.enabled && (
          <form className="buyer-form" onSubmit={disable}>
            <label className="field-label" htmlFor="mfa-disable-password">Nenosiri la akaunti</label>
            <input id="mfa-disable-password" className="text-input" type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
            <label className="field-label" htmlFor="mfa-disable-code">Nambari ya authenticator au recovery code</label>
            <input id="mfa-disable-code" className="text-input" required value={code} onChange={(event) => setCode(event.target.value)} />
            <button className="danger-action" type="submit">Zima 2FA</button>
          </form>
        )}
        {recoveryCodes.length > 0 && (
          <div className="feature-list" role="status">
            <strong>Recovery codes: kila msimbo unatumika mara moja.</strong>
            <ul>{recoveryCodes.map((item) => <li key={item}><code>{item}</code></li>)}</ul>
          </div>
        )}
      </section>
      <section className="form-card settings-form">
        <h2>Vikao vya vifaa</h2>
        <div className="feature-list">
          {sessions.map((session, index) => (
            <article className="seller-card" key={`${session.created_at}-${index}`}>
              <div><strong>{session.current ? "Kifaa hiki" : session.user_agent || "Kifaa kingine"}</strong><span>{session.ip_address || "IP haijarekodiwa"} · Ilitumika: {session.last_seen_at}</span></div>
              {session.current && <span className="seller-status active">Sasa</span>}
            </article>
          ))}
          {!sessions.length && <p className="empty-state">Hakuna kikao kinachoendelea.</p>}
        </div>
        {sessions.length > 1 && <button className="danger-action" type="button" onClick={revokeOtherDevices}>Toka kwenye vifaa vingine</button>}
      </section>
      <section className="form-card settings-form">
        <h2>Mapendeleo ya arifa</h2>
        <form className="buyer-form" onSubmit={saveNotificationPreferences}>
          {[
            ["email_enabled", "Barua pepe"],
            ["sms_enabled", "SMS"],
            ["web_push_enabled", "Web Push"],
            ["mobile_push_enabled", "Mobile Push"],
            ["whatsapp_enabled", "WhatsApp Business"],
            ["security_alerts_enabled", "Tahadhari za usalama"],
            ["marketing_enabled", "Taarifa za matangazo"],
          ].map(([key, label]) => (
            <label className="field-label" key={key}>
              <input
                type="checkbox"
                checked={Boolean(notificationPreferences[key])}
                onChange={(event) => setNotificationPreferences({
                  ...notificationPreferences,
                  [key]: event.target.checked,
                })}
              /> {label}
            </label>
          ))}
          <button className="generate-button" type="submit">Hifadhi mapendeleo</button>
        </form>
      </section>
      <section className="form-card settings-form">
        <h2>Historia ya arifa</h2>
        <div className="feature-list">
          {notificationHistory.map((item) => (
            <article className="seller-card" key={item.id}>
              <div>
                <strong>{item.title}</strong>
                <span>{item.channel} · {item.status}</span>
                <small>{item.body}</small>
                {item.error && <small>{item.error}</small>}
              </div>
            </article>
          ))}
          {!notificationHistory.length && <p className="empty-state">Hakuna historia ya arifa.</p>}
        </div>
      </section>
    </div>
  );
}

function BusinessAssistant({ businessId }) {
  const { language } = useLanguage();
  const [conversations, setConversations] = useState([]);
  const [conversationId, setConversationId] = useState("");
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useFeedbackState("error");

  async function loadConversations() {
    if (!businessId) {
      setConversations([]);
      setConversationId("");
      setMessages([]);
      return;
    }
    const rows = await api(`/businesses/${encodeURIComponent(businessId)}/assistant/conversations`);
    setConversations(rows);
    if (rows.length) {
      setConversationId(rows[0].id);
      const history = await api(`/businesses/${encodeURIComponent(businessId)}/assistant/conversations/${encodeURIComponent(rows[0].id)}`);
      setMessages(history.messages);
    } else {
      setConversationId("");
      setMessages([]);
    }
  }

  useEffect(() => {
    loadConversations().catch((requestError) => setError(requestError.message));
  }, [businessId]);

  async function openConversation(id) {
    setError("");
    try {
      const history = await api(`/businesses/${encodeURIComponent(businessId)}/assistant/conversations/${encodeURIComponent(id)}`);
      setConversationId(id);
      setMessages(history.messages);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function submitQuestion(event) {
    event.preventDefault();
    const text = question.trim();
    if (!text || !businessId || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await api(`/businesses/${encodeURIComponent(businessId)}/assistant/messages`, {
        method: "POST",
        body: JSON.stringify({
          question: text,
          conversation_id: conversationId || null,
        }),
      });
      setConversationId(result.conversation_id);
      setMessages((current) => [
        ...current,
        { id: result.user_message_id, role: "USER", content: result.question },
        {
          id: result.assistant_message_id,
          role: "ASSISTANT",
          content: result.answer,
          period_start: result.period.start,
          period_end: result.period.end,
          sources: result.sources,
          recommendations: result.recommendation_codes,
          ai_status: result.ai_status,
          ai_note: result.ai_note,
        },
      ]);
      setConversations((current) => {
        const existing = current.filter((item) => item.id !== result.conversation_id);
        return [
          { id: result.conversation_id, title: text.slice(0, 120), updated_at: new Date().toISOString() },
          ...existing,
        ];
      });
      setQuestion("");
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  function recommendationLabel(code) {
    const keys = {
      REVIEW_REPLENISHMENT: "assistant.recommendation.reorder",
      COMPARE_BRANCHES: "assistant.recommendation.compareBranches",
    };
    return keys[code] ? translateKey(keys[code], language) : code;
  }

  const sourceKeys = {
    orders: "assistant.source.orders",
    order_items: "assistant.source.orderItems",
    products: "assistant.source.products",
    branches: "assistant.source.branches",
  };

  if (!businessId) {
    return <section className="dashboard-panel"><p>{translateKey("assistant.businessRequired", language)}</p></section>;
  }

  return (
    <section className="dashboard-content business-assistant">
      <header className="assistant-heading">
        <div>
          <h2>{translateKey("assistant.title", language)}</h2>
          <p>{translateKey("assistant.description", language)}</p>
        </div>
        <button type="button" className="table-action" onClick={() => { setConversationId(""); setMessages([]); setError(""); }}>
          {translateKey("assistant.newConversation", language)}
        </button>
      </header>
      <p className="field-hint">{translateKey("assistant.disclosure", language)}</p>
      {error && <p className="form-error" role="alert">{translate(error, language)}</p>}
      <div className="assistant-layout">
        <aside className="dashboard-panel assistant-history">
          <h3>{translateKey("assistant.history", language)}</h3>
          {conversations.map((conversation) => (
            <button
              type="button"
              key={conversation.id}
              className={conversation.id === conversationId ? "assistant-history-active" : ""}
              onClick={() => openConversation(conversation.id)}
            >
              <span data-no-translate="true">{conversation.title}</span>
              <small>{formatDate(conversation.updated_at, language)}</small>
            </button>
          ))}
          {!conversations.length && <p className="empty-state">{translateKey("assistant.noHistory", language)}</p>}
        </aside>
        <div className="dashboard-panel assistant-chat">
          <div className="assistant-messages" aria-live="polite">
            {messages.map((item) => (
              <article className={`assistant-message assistant-message-${item.role.toLowerCase()}`} key={item.id || `${item.role}-${item.created_at}-${item.content}`}>
                <strong>{item.role === "USER" ? translateKey("common.send", language) : translateKey("assistant.title", language)}</strong>
                <p data-no-translate="true">{item.content}</p>
                {item.role === "ASSISTANT" && item.period_start && (
                  <small>{translateKey("assistant.period", language)}: {formatDate(`${item.period_start}T12:00:00Z`, language, { timeStyle: undefined })} – {formatDate(`${item.period_end}T12:00:00Z`, language, { timeStyle: undefined })}</small>
                )}
                {item.role === "ASSISTANT" && item.sources?.length > 0 && (
                  <small>{translateKey("assistant.sources", language)}: {item.sources.map((source) => translateKey(sourceKeys[source] || source, language)).join(", ")}</small>
                )}
                {item.role === "ASSISTANT" && item.recommendations?.length > 0 && (
                  <ul>{item.recommendations.map((code) => <li key={code}>{recommendationLabel(code)}</li>)}</ul>
                )}
                {item.role === "ASSISTANT" && item.ai_status === "NOT_CONFIGURED" && (
                  <small>{translateKey("assistant.notConfigured", language)}</small>
                )}
              </article>
            ))}
            {!messages.length && <p className="empty-state">{translateKey("assistant.description", language)}</p>}
          </div>
          <form className="assistant-question" onSubmit={submitQuestion}>
            <label className="sr-only" htmlFor="business-assistant-question">{translateKey("assistant.title", language)}</label>
            <textarea
              id="business-assistant-question"
              className="text-input"
              maxLength={1000}
              required
              value={question}
              placeholder={translateKey("assistant.placeholder", language)}
              onChange={(event) => setQuestion(event.target.value)}
            />
            <button className="generate-button" type="submit" disabled={busy || !question.trim()}>
              {busy ? translateKey("assistant.loading", language) : translateKey("assistant.send", language)}
            </button>
          </form>
        </div>
      </div>
    </section>
  );
}

function MarketplaceDashboard({ user, onLogout }) {
  const localize = useLocalize();
  const { language, backgroundColor, setBackgroundColor } = useLanguage();
  const { notify } = useNotification();
  const [tab, setTab] = useState("overview");
  useEffect(() => {
    function openRequestedSettings() {
      if (window.location.hash === "#settings") setTab("settings");
      else if (!window.location.hash) setTab("overview");
    }
    function openCustomSettings() {
      setTab("settings");
    }
    openRequestedSettings();
    window.addEventListener("hashchange", openRequestedSettings);
    window.addEventListener("liparahisi-open-settings", openCustomSettings);
    return () => {
      window.removeEventListener("hashchange", openRequestedSettings);
      window.removeEventListener("liparahisi-open-settings", openCustomSettings);
    };
  }, []);
  const [stats, setStats] = useState(null);
  const [products, setProducts] = useState([]);
  const [sales, setSales] = useState([]);
  const [adminSummary, setAdminSummary] = useState(null);
  const [sellers, setSellers] = useState([]);
  const [auditRows, setAuditRows] = useState([]);
  const [securityAlerts, setSecurityAlerts] = useState([]);
  const [securityAppeals, setSecurityAppeals] = useState([]);
  const [securityAlertDetail, setSecurityAlertDetail] = useState(null);
  const [mySecurityAlerts, setMySecurityAlerts] = useState([]);
  const [appealDrafts, setAppealDrafts] = useState({});
  const [securityFilters, setSecurityFilters] = useState({ status: "", severity: "", category: "" });
  const [securityActionNote, setSecurityActionNote] = useState("");
  const [securityAppealNote, setSecurityAppealNote] = useState("");
  const [adminDocuments, setAdminDocuments] = useState([]);
  const [systemHealth, setSystemHealth] = useState(null);
  const [notificationSettings, setNotificationSettings] = useState(null);
  const [sellerKyc, setSellerKyc] = useState(null);
  const [sellerKycRows, setSellerKycRows] = useState([]);
  const [kycForm, setKycForm] = useState({
    seller_type: "INDIVIDUAL",
    phone: user.phone || "",
    identity_name: "",
    identity_number: "",
    business_name: "",
    business_registration_number: "",
    tin_number: "",
    business_license_number: "",
    business_address: "",
  });
  const [businesses, setBusinesses] = useState([]);
  const [selectedBusinessId, setSelectedBusinessId] = useState("");
  const [businessSummary, setBusinessSummary] = useState(null);
  const [businessForm, setBusinessForm] = useState({
    name: "",
    business_type: "Supermarket",
    address: "",
    phone: "",
    email: "",
  });
  const [branchForm, setBranchForm] = useState({
    name: "",
    address: "",
    phone: "",
    email: "",
  });
  const [staffForm, setStaffForm] = useState({
    email: "",
    role: "STAFF",
    branch_id: "",
  });
  const [documents, setDocuments] = useState([]);
  const [documentKind, setDocumentKind] = useState("seller_verification");
  const [documentProductId, setDocumentProductId] = useState("");
  const [categories, setCategories] = useState([]);
  const [lowStock, setLowStock] = useState([]);
  const [stockProductId, setStockProductId] = useState("");
  const [stockMovements, setStockMovements] = useState([]);
  const [cancellations, setCancellations] = useState([]);
  const [returnRequests, setReturnRequests] = useState([]);
  const [payoutData, setPayoutData] = useState({ accounts: [], entries: [] });
  const [reviews, setReviews] = useState([]);
  const [tickets, setTickets] = useState([]);
  const [systemAlerts, setSystemAlerts] = useState([]);
  const [coupons, setCoupons] = useState([]);
  const [couponForm, setCouponForm] = useState({
    code: "",
    discount_type: "PERCENT",
    discount_value: "10",
    max_redemptions: "",
  });
  const [payoutForm, setPayoutForm] = useState({
    seller_id: "",
    amount: "",
    payment_method: "MOBILE_MONEY",
    payment_reference: "",
    note: "",
  });
  const [productForm, setProductForm] = useState({
    name: "", description: "", image_url: "", category: "", amount: "",
    cost_amount: "", stock_quantity: "", low_stock_threshold: "5",
  });
  const [productVariants, setProductVariants] = useState([]);
  const [productImages, setProductImages] = useState([]);
  const productImageInputRef = useRef(null);
  const [productVideos, setProductVideos] = useState([]);
  const productVideoInputRef = useRef(null);
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
    const [dashboard, productRows, salesRows, payouts] = await Promise.all([
      api("/dashboard"),
      api(`/products?q=${encodeURIComponent(productQuery)}`),
      api(`/sales?${salesParams.toString()}`),
      api("/payouts"),
    ]);
    setStats(dashboard);
    setProducts(productRows);
    setSales(salesRows);
    setPayoutData(payouts);
    if (!seenSalesRef.current) {
      seenSalesRef.current = new Set(salesRows.map((sale) => sale.order_id));
    }
    setCategories(await api("/categories"));
    setLowStock(await api("/inventory/low-stock"));
    if (!isAdmin) {
      const [documentRows, kycStatus, businessRows, securityRows] = await Promise.all([
        api("/documents"),
        api("/auth/kyc"),
        api("/businesses"),
        api("/security/alerts"),
      ]);
      setDocuments(documentRows);
      setSellerKyc(kycStatus);
      setMySecurityAlerts(securityRows);
      setBusinesses(businessRows);
      if (!selectedBusinessId && businessRows.length) {
        setSelectedBusinessId(businessRows[0].id);
      }
    }
    if (isAdmin) setAuditRows(await api("/admin/audit"));
    if (isAdmin) {
      const securityQuery = new URLSearchParams(
        Object.entries(securityFilters).filter(([, value]) => value),
      );
      const [
        sellerRows,
        cancellationRows,
        returnRows,
        reviewRows,
        ticketRows,
        documentRows,
        health,
        notificationConfig,
        kycRows,
        securityAlertRows,
        appealRows,
        dashboardSummary,
        couponRows,
        systemAlertRows,
      ] = await Promise.all([
        api("/admin/sellers"),
        api("/admin/cancellations"),
        api("/admin/returns"),
        api("/admin/reviews"),
        api("/admin/support-tickets"),
        api("/admin/documents"),
        api("/admin/health/system"),
        api("/admin/notification-settings"),
        api("/admin/kyc"),
        api(`/admin/security/alerts?${securityQuery.toString()}`),
        api("/admin/security/appeals"),
        api("/admin/dashboard"),
        api("/admin/coupons"),
        api("/admin/system-alerts"),
      ]);
      setSellers(sellerRows);
      setCancellations(cancellationRows);
      setReturnRequests(returnRows);
      setReviews(reviewRows);
      setTickets(ticketRows);
      setAdminDocuments(documentRows);
      setSystemHealth(health);
      setNotificationSettings(notificationConfig);
      setSellerKycRows(kycRows);
      setSecurityAlerts(securityAlertRows);
      setSecurityAppeals(appealRows);
      setAdminSummary(dashboardSummary);
      setCoupons(couponRows);
      setSystemAlerts(systemAlertRows);
      if (!seenAdminEventsRef.current) {
        seenAdminEventsRef.current = new Set(adminEventKeys([
          ["seller", sellerRows],
          ["cancellation", cancellationRows],
          ["return", returnRows],
          ["review", reviewRows],
          ["ticket", ticketRows],
          ["alert", alertRows],
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
        const [dashboard, latestSales, payouts, adminData] = await Promise.all([
          api("/dashboard"),
          api(`/sales?${params.toString()}`),
          api("/payouts"),
          isAdmin ? Promise.all([
            api("/admin/sellers"),
            api("/admin/cancellations"),
            api("/admin/returns"),
            api("/admin/reviews"),
            api("/admin/support-tickets"),
            api("/admin/system-alerts"),
          ]) : Promise.resolve(null),
        ]);
        if (active) {
          setStats(dashboard);
          setSales(latestSales);
          setPayoutData(payouts);
          const knownSales = seenSalesRef.current || new Set();
          const newSales = latestSales.filter((sale) => !knownSales.has(sale.order_id));
          latestSales.forEach((sale) => knownSales.add(sale.order_id));
          seenSalesRef.current = knownSales;
          if (newSales.length) notify("Oda mpya imepokelewa.", "info");
          if (adminData) {
            const [sellerRows, cancellationRows, returnRows, reviewRows, ticketRows, alertRows] = adminData;
            setSellers(sellerRows);
            setCancellations(cancellationRows);
            setReturnRequests(returnRows);
            setReviews(reviewRows);
            setTickets(ticketRows);
            setSystemAlerts(alertRows);
            const eventRows = [
              ["seller", sellerRows],
              ["cancellation", cancellationRows],
              ["return", returnRows],
              ["review", reviewRows],
              ["ticket", ticketRows],
              ["alert", alertRows],
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

  function addProductVariant() {
    setProductVariants((current) => [...current, { label: "", stock_quantity: "0" }]);
  }

  function updateProductVariant(index, field, value) {
    setProductVariants((current) => current.map((variant, variantIndex) => (
      variantIndex === index ? { ...variant, [field]: value } : variant
    )));
  }

  function removeProductVariant(index) {
    setProductVariants((current) => current.filter((_, variantIndex) => variantIndex !== index));
  }

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
      if (productVideos.length > maxProductVideos) {
        throw new Error(translate("Bidhaa inaweza kuwa na video 3 tu.", language));
      }
      productVideos.forEach(validateProductVideo);
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
          variants: productVariants.map((variant) => ({
            label: variant.label.trim(),
            stock_quantity: Number(variant.stock_quantity) || 0,
          })),
        }),
      });
      setProductForm({ name: "", description: "", image_url: "", category: "", amount: "", cost_amount: "", stock_quantity: "", low_stock_threshold: "5" });
      setProductVariants([]);
      setProductImages([]);
      setProductVideos([]);
      if (productImageInputRef.current) productImageInputRef.current.value = "";
      if (productVideoInputRef.current) productVideoInputRef.current.value = "";
      if (productImages.length) {
        try {
          await uploadProductGallery(createdProduct.id, productImages);
        } catch (uploadError) {
          setError(`Bidhaa imehifadhiwa, lakini picha hazikupakiwa: ${uploadError.message}`);
          await refresh();
          return;
        }
      }
      if (productVideos.length) {
        try {
          for (const file of productVideos) {
            await uploadProductVideo(createdProduct.id, file);
          }
        } catch (uploadError) {
          setError(`Bidhaa imehifadhiwa, lakini video hazikupakiwa: ${uploadError.message}`);
          await refresh();
          return;
        }
      }
      setMessage(productImages.length && productVideos.length
        ? "Bidhaa, picha na video zimehifadhiwa."
        : productImages.length
          ? "Bidhaa na picha zake zimehifadhiwa."
          : productVideos.length
            ? "Bidhaa na video zimehifadhiwa."
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

  async function updateProductVariantStock(productId, variantId, stockQuantity) {
    setError("");
    setMessage("");
    try {
      await api(`/products/${encodeURIComponent(productId)}/variants/${encodeURIComponent(variantId)}`, {
        method: "PATCH",
        body: JSON.stringify({ stock_quantity: stockQuantity }),
      });
      setMessage(translate("Stock ya variant imehifadhiwa.", language));
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function addExistingProductVariant(productId, label, stockQuantity) {
    setError("");
    setMessage("");
    try {
      await api(`/products/${encodeURIComponent(productId)}/variants`, {
        method: "POST",
        body: JSON.stringify({ label, stock_quantity: stockQuantity }),
      });
      setMessage(translate("Variant imeongezwa.", language));
      await refresh();
      return true;
    } catch (requestError) {
      setError(requestError.message);
      return false;
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

  async function uploadExistingProductVideos(productId, files) {
    setError("");
    setMessage("");
    let uploadedCount = 0;
    try {
      files.forEach(validateProductVideo);
      const currentCount = products.find((product) => product.id === productId)?.videos?.length || 0;
      if (currentCount + files.length > maxProductVideos) {
        throw new Error(translate("Bidhaa inaweza kuwa na video 3 tu.", language));
      }
      for (const file of files) {
        await uploadProductVideo(productId, file);
        uploadedCount += 1;
      }
      setMessage(translate("Video zimehifadhiwa.", language));
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      if (uploadedCount > 0) {
        await refresh().catch((refreshError) => setError(refreshError.message));
      }
    }
  }

  async function deleteProductVideo(productId, videoId) {
    setError("");
    setMessage("");
    try {
      await api(
        `/products/${encodeURIComponent(productId)}/videos/${encodeURIComponent(videoId)}`,
        { method: "DELETE" },
      );
      setMessage(translate("Video imeondolewa.", language));
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

  async function uploadProfileImage(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    const file = event.currentTarget.elements.profile_image?.files?.[0];
    if (!file) {
      setError(translate("Chagua picha ya profile.", language));
      return;
    }
    const formData = new FormData();
    formData.append("image", file);
    try {
      await api("/account/profile-image", { method: "POST", body: formData });
      setMessage(translate("Picha ya profile imehifadhiwa.", language));
      await refresh();
      window.location.reload();
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

  async function uploadDocument(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const file = form.elements.document.files?.[0];
    if (!file) return;
    try {
      validateDocument(file);
      const formData = new FormData();
      const selectedKind = form.elements["document-kind"]?.value || documentKind;
      formData.append("kind", selectedKind);
      if (selectedKind === "product_document") {
        formData.append("product_id", documentProductId);
      }
      formData.append("document", file);
      await api("/documents", { method: "POST", body: formData });
      form.reset();
      setMessage("Nyaraka imehifadhiwa kwa usalama.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function refreshSystemHealth() {
    try {
      setSystemHealth(await api("/admin/health/system"));
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function refreshSecurityData() {
    try {
      const query = new URLSearchParams(
        Object.entries(securityFilters).filter(([, value]) => value),
      );
      const [alertRows, appealRows] = await Promise.all([
        api(`/admin/security/alerts?${query.toString()}`),
        api("/admin/security/appeals"),
      ]);
      setSecurityAlerts(alertRows);
      setSecurityAppeals(appealRows);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function runSecurityScan() {
    try {
      const result = await api("/admin/security/scan", { method: "POST" });
      const ai = result.ai_analysis;
      setMessage(`Rule scan imekamilika: alerts ${result.alerts_updated}. OpenAI: ${ai.status}, zilizochambuliwa ${ai.analyzed}, zilizoachwa ${ai.skipped}.`);
      setError(ai.status === "FAILED"
        ? "OpenAI haikukamilisha uchambuzi; rule alerts zimehifadhiwa na hakuna enforcement iliyofanyika."
        : "");
      await refreshSecurityData();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function openSecurityAlert(alertId) {
    try {
      setSecurityAlertDetail(await api(`/admin/security/alerts/${encodeURIComponent(alertId)}`));
      setSecurityActionNote("");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function actOnSecurityAlert(action) {
    if (!securityAlertDetail || securityActionNote.trim().length < 2) {
      setError("Andika sababu ya hatua kabla ya kuendelea.");
      return;
    }
    try {
      const result = await api(`/admin/security/alerts/${encodeURIComponent(securityAlertDetail.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ action, note: securityActionNote.trim() }),
      });
      setMessage(action === "WARN" && !result.notification_queued
        ? "Onyo limehifadhiwa, lakini hakuna provider iliyokubali arifa. Mtumiaji bado anaweza kuona tahadhari ndani ya akaunti."
        : action === "WARN"
          ? `Onyo limehifadhiwa; arifa imewekwa kwenye queue: ${result.notification_channels.join(", ")}.`
          : "Hatua ya usalama imehifadhiwa kwenye audit trail.");
      setSecurityAlertDetail(null);
      setSecurityActionNote("");
      await refreshSecurityData();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function submitSecurityAppeal(alertId) {
    const messageText = (appealDrafts[alertId] || "").trim();
    if (messageText.length < 10) {
      setError("Andika maelezo ya appeal yenye angalau herufi 10.");
      return;
    }
    try {
      await api(`/security/alerts/${encodeURIComponent(alertId)}/appeal`, {
        method: "POST",
        body: JSON.stringify({ message: messageText }),
      });
      setMessage("Appeal imetumwa kwa ukaguzi wa admin.");
      setAppealDrafts({ ...appealDrafts, [alertId]: "" });
      setMySecurityAlerts(await api("/security/alerts"));
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function reviewSecurityAppeal(appealId, status) {
    if (securityAppealNote.trim().length < 2) {
      setError("Andika sababu ya uamuzi wa appeal.");
      return;
    }
    try {
      await api(`/admin/security/appeals/${encodeURIComponent(appealId)}`, {
        method: "PATCH",
        body: JSON.stringify({ status, note: securityAppealNote.trim() }),
      });
      setMessage(status === "ACCEPTED"
        ? "Appeal imekubaliwa na alert imerudishwa kwenye ukaguzi wa admin."
        : "Appeal imekataliwa kwa uamuzi wa admin.");
      setSecurityAppealNote("");
      await refreshSecurityData();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function updateDelivery(
    orderId,
    status,
    deliveryCarrier = "",
    trackingCode = "",
    deliveryFee = 0,
  ) {
    await api(`/orders/${encodeURIComponent(orderId)}/delivery`, {
      method: "PUT",
      body: JSON.stringify({
        delivery_status: status,
        delivery_carrier: deliveryCarrier,
        tracking_code: trackingCode,
        delivery_fee: deliveryFee,
      }),
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

  async function submitKyc(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    try {
      const result = await api("/auth/kyc", {
        method: "POST",
        body: JSON.stringify(kycForm),
      });
      setSellerKyc(result);
      setMessage("Maombi ya KYC yametumwa kwa ukaguzi.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function deleteKyc() {
    setError("");
    setMessage("");
    try {
      await api("/auth/kyc", { method: "DELETE" });
      setSellerKyc({ status: "NOT_VERIFIED", profile: null, documents: [], history: [] });
      setMessage("Maombi ya KYC na nyaraka zake zimefutwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function reviewKyc(sellerId, status, reason) {
    setError("");
    setMessage("");
    try {
      await api(`/admin/kyc/${encodeURIComponent(sellerId)}`, {
        method: "PATCH",
        body: JSON.stringify({
          status,
          reason,
          expires_at: "",
        }),
      });
      setMessage(`Maamuzi ya KYC ya ${status} yamerekodiwa.`);
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function createBusiness(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    try {
      const result = await api("/businesses", {
        method: "POST",
        body: JSON.stringify(businessForm),
      });
      setBusinessForm({
        name: "",
        business_type: "Supermarket",
        address: "",
        phone: "",
        email: "",
      });
      setSelectedBusinessId(result.id);
      setMessage("Biashara na tawi kuu zimeundwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function loadBusinessSummary(businessId) {
    setSelectedBusinessId(businessId);
    try {
      setBusinessSummary(await api(`/businesses/${encodeURIComponent(businessId)}/summary`));
      setError("");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function createBranch(event) {
    if (!selectedBusinessId) return;
    event.preventDefault();
    setError("");
    setMessage("");
    try {
      await api(`/businesses/${encodeURIComponent(selectedBusinessId)}/branches`, {
        method: "POST",
        body: JSON.stringify({ ...branchForm, is_active: true }),
      });
      setBranchForm({ name: "", address: "", phone: "", email: "" });
      setMessage("Tawi limeongezwa.");
      await loadBusinessSummary(selectedBusinessId);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function createStaff(event) {
    event.preventDefault();
    if (!selectedBusinessId) return;
    setError("");
    setMessage("");
    try {
      await api(`/businesses/${encodeURIComponent(selectedBusinessId)}/memberships`, {
        method: "POST",
        body: JSON.stringify({
          email: staffForm.email,
          role: staffForm.role,
          branch_id: staffForm.branch_id || null,
        }),
      });
      setStaffForm({ email: "", role: "STAFF", branch_id: "" });
      setMessage("Wafanyakazi wameongezwa kwenye biashara.");
      await refresh();
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

  async function resolveReturnRequest(id, status) {
    try {
      await api(`/admin/returns/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status, admin_note: "" }),
      });
      setMessage("Uamuzi wa marejesho umehifadhiwa; hakuna pesa iliyohamishwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function acknowledgeSystemAlert(id) {
    try {
      await api(`/admin/system-alerts/${encodeURIComponent(id)}`, { method: "PATCH" });
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

  async function createCoupon(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    try {
      await api("/admin/coupons", {
        method: "POST",
        body: JSON.stringify({
          ...couponForm,
          discount_value: Number(couponForm.discount_value),
          max_redemptions: couponForm.max_redemptions
            ? Number(couponForm.max_redemptions)
            : null,
        }),
      });
      setCouponForm({
        code: "",
        discount_type: "PERCENT",
        discount_value: "10",
        max_redemptions: "",
      });
      setMessage("Kuponi imeundwa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function changeCouponStatus(coupon, isActive) {
    try {
      await api(`/admin/coupons/${coupon.id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_active: isActive }),
      });
      await refresh();
      setMessage("Hali ya kuponi imehifadhiwa.");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function recordManualPayout(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    const sellerId = payoutForm.seller_id || payoutData.accounts[0]?.seller_id;
    if (!sellerId) {
      setError("Hakuna muuzaji mwenye akaunti ya malipo.");
      return;
    }
    try {
      await api("/payouts", {
        method: "POST",
        body: JSON.stringify({
          ...payoutForm,
          seller_id: sellerId,
          amount: Number(payoutForm.amount),
        }),
      });
      setPayoutForm((current) => ({
        ...current,
        seller_id: sellerId,
        amount: "",
        payment_reference: "",
        note: "",
      }));
      setMessage("Rekodi ya malipo imehifadhiwa; mfumo haukutuma pesa.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  const selectedPayoutAccount = payoutData.accounts.find(
    (account) => account.seller_id === payoutForm.seller_id,
  ) || payoutData.accounts[0];
  const nav = [
    ["overview", "Muhtasari"],
    ["products", "Bidhaa"],
    ["sales", "Mauzo"],
    ["payouts", isAdmin ? "Malipo ya wauzaji" : "Malipo yangu"],
    ...(!isAdmin ? [["storefront", "Duka lako"], ["stock", "Stock"]] : []),
    ...(!isAdmin ? [["business", "Biashara na Matawi"], ["assistant", translateKey("assistant.title", language)]] : []),
    ...(isAdmin ? [["sellers", "Wauzaji"]] : []),
    ...(isAdmin ? [["kyc", "Ukaguzi wa KYC"]] : []),
    ...(!isAdmin ? [["kyc", "KYC ya duka"]] : []),
    ...(isAdmin ? [["categories", "Makundi"], ["cancellations", "Kughairi"], ["reviews", "Maoni"], ["support", "Msaada"]] : []),
    ...(isAdmin ? [["audit", "Audit"]] : []),
    ...(isAdmin ? [["security", "Usalama na fraud"]] : []),
    ...(!isAdmin ? [["security", "Tahadhari za usalama"]] : []),
    ...(isAdmin ? [["documents", "Nyaraka"]] : []),
    ...(isAdmin ? [["health", "Afya ya mfumo"]] : []),
    ...(isAdmin ? [["notifications", "Arifa"]] : []),
    ...(isAdmin ? [["alerts", `Arifa za mfumo${systemAlerts.some((alert) => !alert.acknowledged_at) ? ` (${systemAlerts.filter((alert) => !alert.acknowledged_at).length})` : ""}`], ["policies", "Sera (rasimu)"]] : []),
    ...(isAdmin ? [["coupons", "Kuponi"], ["returns", "Marejesho"]] : []),
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
      <div className="dashboard-layout">
        <aside className="dashboard-sidebar">
          <div className="dashboard-sidebar-heading">
            <span>{isAdmin ? "Admin" : "Muuzaji"}</span>
            <strong>{user.shop_name || user.full_name}</strong>
          </div>
          <nav className="dashboard-tabs" aria-label="Sehemu za dashboard">
            {nav.map(([key, title]) => (
              <button key={key} type="button" className={tab === key ? "active" : ""} onClick={() => { setTab(key); setError(""); setMessage(""); if (window.location.hash === "#settings") window.history.replaceState(null, "", window.location.pathname + window.location.search); }}>
                {title}
              </button>
            ))}
          </nav>
        </aside>
        <div className="dashboard-workspace">
          {error && <p className="form-error dashboard-alert" role="alert">{error}</p>}
          {message && <p className="success-message dashboard-alert" role="status">{message}</p>}

      {tab === "assistant" && !isAdmin && <BusinessAssistant businessId={selectedBusinessId} />}

      {tab === "overview" && stats && (
        <section className="dashboard-content">
          {isAdmin ? (
            adminSummary ? (
              <>
                <div className="stats-grid">
                  <StatCard title="Watumiaji wote" value={adminSummary.counts.total_users} />
                  <StatCard title="Wanunuzi" value={adminSummary.counts.buyers} />
                  <StatCard title="Wauzaji" value={adminSummary.counts.sellers} />
                  <StatCard title="Wauzaji waliothibitishwa" value={adminSummary.counts.verified_sellers} />
                  <StatCard title="KYC inayosubiri" value={adminSummary.counts.pending_kyc} note="Pending, review au resubmission." />
                  <StatCard title="Biashara" value={adminSummary.counts.businesses} />
                  <StatCard title="Bidhaa" value={adminSummary.counts.products} />
                  <StatCard title="Oda zote" value={adminSummary.counts.orders} />
                  <StatCard title="Deliveries hai" value={adminSummary.counts.active_deliveries} />
                  <StatCard title="Tahadhari za fraud" value={adminSummary.counts.fraud_alerts} />
                  <StatCard title="Matukio ya usalama (saa 24)" value={adminSummary.counts.security_events} />
                  <StatCard title="Mauzo yaliyokamilika (demo)" value={`TZS ${formatMoney(stats.gross_sales)}`} />
                </div>
                <div className="dashboard-grid">
                  <DashboardTrendChart title="Mauzo ya siku 7 (demo)" rows={adminSummary.sales_trend} />
                  <DashboardStatusList title="Utendaji wa delivery" rows={adminSummary.delivery_statuses} />
                  <DashboardActivityFeed rows={adminSummary.activity} />
                  <section className="dashboard-panel">
                    <h2>Mifumo na tahadhari</h2>
                    <p>Afya ya mfumo: <strong>{systemHealth?.status || "Tazama afya ya mfumo"}</strong></p>
                    <p>Fraud alerts na matukio ya usalama vinahitaji ukaguzi wa binadamu.</p>
                    <div className="dashboard-panel-actions">
                      <button type="button" className="table-action" onClick={() => setTab("security")}>Kagua usalama</button>
                      <button type="button" className="table-action" onClick={() => setTab("kyc")}>Kagua KYC</button>
                      <button type="button" className="table-action" onClick={() => setTab("audit")}>Fungua audit logs</button>
                    </div>
                  </section>
                </div>
              </>
            ) : <p className="empty-state">Inapakia muhtasari wa mfumo...</p>
          ) : (
            <>
              <div className="stats-grid">
                <StatCard title="Mauzo yaliyokamilika (demo)" value={`TZS ${formatMoney(stats.gross_sales)}`} />
                <StatCard title="Faida halisi (demo)" value={`TZS ${formatMoney(stats.net_profit)}`} />
                <StatCard title="Oda zote" value={stats.total_orders} />
                <StatCard title="Deliveries hai" value={stats.active_deliveries} />
                <StatCard title="Bidhaa zilizo hai" value={stats.product_count} />
                <StatCard title="Bidhaa zenye stock ndogo" value={stats.low_stock_count || 0} />
                <StatCard title="Wateja" value={stats.customer_count} />
                <StatCard title="Maoni ya bidhaa" value={stats.review_count} />
                <StatCard title="Vipande vya stock" value={stats.inventory_units} />
                <StatCard title="Maombi ya kughairi" value={stats.pending_cancellations} />
                <StatCard title="Kamisheni ya jukwaa (5%)" value={`TZS ${formatMoney(stats.platform_fees)}`} />
                <StatCard title="Hali ya KYC" value={sellerKyc?.status || user.kyc_status || "NOT_VERIFIED"} />
              </div>
              <div className="dashboard-grid">
                <DashboardTrendChart title="Mauzo ya siku 7 (demo)" rows={stats.sales_trend} />
                <section className="dashboard-panel">
                  <h2>Arifa za duka</h2>
                  <p>Stock chini: <strong>{lowStock.length}</strong></p>
                  <p>Tahadhari za akaunti: <strong>{mySecurityAlerts.length}</strong></p>
                  <p>Matawi yanayoonekana: <strong>{businessSummary?.branches?.length || 0}</strong></p>
                  {businessSummary?.branches?.length > 0 && (
                    <div className="dashboard-status-list">
                      {businessSummary.branches.map((branch) => (
                        <div className="dashboard-status-row" key={branch.branch_id}>
                          <span>{branch.branch_name}</span>
                          <strong>{branch.order_count} oda</strong>
                        </div>
                      ))}
                    </div>
                  )}
                  {lowStock.slice(0, 4).map((product) => (
                    <div className="dashboard-status-row" key={product.id}>
                      <span>{product.name}</span><strong>{product.stock_quantity} zimebaki</strong>
                    </div>
                  ))}
                </section>
              </div>
            </>
          )}
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
                <label className="field-label" htmlFor="product-video-files">{translate("Pakia video za bidhaa (hiari)", language)}</label>
                <input
                  id="product-video-files"
                  ref={productVideoInputRef}
                  className="text-input"
                  type="file"
                  accept="video/mp4,video/webm"
                  multiple
                  onChange={(event) => setProductVideos(Array.from(event.target.files || []))}
                />
                <p className="field-hint">{translate("Chagua hadi video 3 za MP4 au WebM; kila video hadi MB 20.", language)}</p>
                <label className="field-label" htmlFor="product-category">Kundi</label>
                <input id="product-category" className="text-input" list="product-categories" maxLength={60} value={productForm.category} onChange={(event) => setProductForm({ ...productForm, category: event.target.value })} />
                <datalist id="product-categories">{categories.map((item) => <option key={item.id} value={item.name} />)}</datalist>
                <label className="field-label" htmlFor="product-amount">Bei ya kuuza (TZS)</label>
                <input id="product-amount" className="text-input" type="number" min="1" step="1" required value={productForm.amount} onChange={(event) => setProductForm({ ...productForm, amount: event.target.value })} />
                <label className="field-label" htmlFor="product-cost">Gharama ya bidhaa (TZS)</label>
                <input id="product-cost" className="text-input" type="number" min="0" step="1" value={productForm.cost_amount} onChange={(event) => setProductForm({ ...productForm, cost_amount: event.target.value })} />
                {productVariants.length ? (
                  <p className="field-hint">
                    {translate("Stock ya jumla kutoka variants", language)}: {productVariants.reduce((sum, variant) => sum + (Number(variant.stock_quantity) || 0), 0)}
                  </p>
                ) : (
                  <>
                    <label className="field-label" htmlFor="product-stock">Stock inayopatikana</label>
                    <input id="product-stock" className="text-input" type="number" min="0" step="1" required value={productForm.stock_quantity} onChange={(event) => setProductForm({ ...productForm, stock_quantity: event.target.value })} />
                  </>
                )}
                <div className="variant-editor">
                  <strong>Variants (hiari)</strong>
                  <p className="field-hint">Kila variant hutumia bei ya bidhaa na ina stock yake.</p>
                  {productVariants.map((variant, index) => (
                    <div className="variant-editor-row" key={`variant-${index}`}>
                      <input
                        className="text-input"
                        aria-label={`${translate("Variant", language)} ${index + 1}`}
                        placeholder={translate("Mfano: Rangi nyeusi / Size M", language)}
                        maxLength={80}
                        required
                        value={variant.label}
                        onChange={(event) => updateProductVariant(index, "label", event.target.value)}
                      />
                      <input
                        className="text-input"
                        aria-label={`${translate("Stock kwa variant", language)} ${index + 1}`}
                        type="number"
                        min="0"
                        max="999999999"
                        required
                        value={variant.stock_quantity}
                        onChange={(event) => updateProductVariant(index, "stock_quantity", event.target.value)}
                      />
                      <button className="table-action" type="button" onClick={() => removeProductVariant(index)}>Ondoa variant</button>
                    </div>
                  ))}
                  <button className="table-action" type="button" onClick={addProductVariant}>Ongeza variant</button>
                </div>
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
                  onUploadVideos={uploadExistingProductVideos}
                  onDeleteImage={deleteProductImage}
                  onDeleteGalleryImage={deleteProductGalleryImage}
                  onDeleteVideo={deleteProductVideo}
                  onAddVariant={addExistingProductVariant}
                  onUpdateVariantStock={updateProductVariantStock}
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
              <option value="">{translate("Hali zote", language)}</option>
              {["PENDING", "COMPLETED", "EXPIRED", "CANCELLED"].map((status) => (
                <option key={status} value={status}>{translate(status, language)}</option>
              ))}
            </select>
            <button type="submit" className="table-action">Chuja</button>
            <a className="table-action" href="/api/admin/export/sales">Pakua mauzo CSV</a>
          </form>
          <SalesTable sales={sales} isAdmin={isAdmin} onArchive={archiveSale} onUpdateDelivery={updateDelivery} />
          {!isAdmin && <p className="field-hint">Ukificha rekodi, itaondoka kwenye orodha yako lakini admin ataendelea kuiona.</p>}
        </section>
      )}

      {tab === "payouts" && (
        <section className="dashboard-content feature-list">
          <div className="dashboard-section-heading">
            <h2>{isAdmin ? "Malipo ya wauzaji" : "Malipo yangu"}</h2>
            <span>{payoutData.accounts.length} akaunti</span>
          </div>
          <p className="field-hint">Salio linatokana na oda zilizokamilika ukiondoa malipo yaliyorekodiwa. Rekodi haianzi uhamisho wa pesa.</p>
          <div className="feature-columns">
            <div className="feature-list">
              {payoutData.accounts.map((account) => (
                <article className="seller-card" key={account.seller_id}>
                  <div>
                    <strong>{account.shop_name || account.seller_name}</strong>
                    <span>{account.seller_name}</span>
                    <small>Mapato yaliyokamilika: TZS {formatMoney(account.completed_earned)}</small>
                    <small>Malipo yaliyorekodiwa: TZS {formatMoney(account.recorded_paid)}</small>
                    <small>Salio linalodaiwa: TZS {formatMoney(account.outstanding_amount)}</small>
                    {account.overpaid_amount > 0 && <small>Malipo yanayozidi salio: TZS {formatMoney(account.overpaid_amount)}</small>}
                  </div>
                </article>
              ))}
              {!payoutData.accounts.length && <p className="empty-state">Hakuna akaunti ya malipo ya muuzaji.</p>}
            </div>
            {isAdmin && (
              <form className="form-card" onSubmit={recordManualPayout}>
                <h3>Rekodi malipo yaliyofanywa nje ya mfumo</h3>
                <label className="field-label" htmlFor="payout-seller">Muuzaji</label>
                <select
                  id="payout-seller"
                  className="text-input"
                  required
                  value={payoutForm.seller_id || selectedPayoutAccount?.seller_id || ""}
                  onChange={(event) => setPayoutForm({ ...payoutForm, seller_id: event.target.value })}
                >
                  {payoutData.accounts.map((account) => (
                    <option key={account.seller_id} value={account.seller_id}>
                      {account.shop_name || account.seller_name} · TZS {formatMoney(account.outstanding_amount)}
                    </option>
                  ))}
                </select>
                <label className="field-label" htmlFor="payout-amount">Kiasi (TZS)</label>
                <input
                  id="payout-amount"
                  className="text-input"
                  type="number"
                  min="1"
                  max={selectedPayoutAccount?.outstanding_amount || 0}
                  step="1"
                  required
                  value={payoutForm.amount}
                  onChange={(event) => setPayoutForm({ ...payoutForm, amount: event.target.value })}
                />
                <label className="field-label" htmlFor="payout-method">Njia ya malipo</label>
                <select
                  id="payout-method"
                  className="text-input"
                  value={payoutForm.payment_method}
                  onChange={(event) => setPayoutForm({ ...payoutForm, payment_method: event.target.value })}
                >
                  <option value="MOBILE_MONEY">MOBILE_MONEY</option>
                  <option value="BANK_TRANSFER">BANK_TRANSFER</option>
                  <option value="CASH">CASH</option>
                  <option value="OTHER">OTHER</option>
                </select>
                <label className="field-label" htmlFor="payout-reference">Rejea ya muamala</label>
                <input
                  id="payout-reference"
                  className="text-input"
                  minLength={3}
                  maxLength={100}
                  required
                  value={payoutForm.payment_reference}
                  onChange={(event) => setPayoutForm({ ...payoutForm, payment_reference: event.target.value })}
                />
                <label className="field-label" htmlFor="payout-note">Maelezo (hiari)</label>
                <input
                  id="payout-note"
                  className="text-input"
                  maxLength={500}
                  value={payoutForm.note}
                  onChange={(event) => setPayoutForm({ ...payoutForm, note: event.target.value })}
                />
                <button
                  className="generate-button"
                  type="submit"
                  disabled={
                    !selectedPayoutAccount
                    || selectedPayoutAccount.outstanding_amount <= 0
                    || !Number.isInteger(Number(payoutForm.amount))
                    || Number(payoutForm.amount) <= 0
                    || Number(payoutForm.amount) > selectedPayoutAccount.outstanding_amount
                  }
                >
                  Rekodi malipo yaliyofanywa
                </button>
                <p className="field-hint">Hakikisha malipo yamefanywa kwanza; usiweke PIN, nenosiri, au taarifa za siri za akaunti.</p>
              </form>
            )}
          </div>
          <div className="feature-list">
            <h3>Historia ya malipo</h3>
            {payoutData.entries.map((entry) => (
              <article className="seller-card" key={entry.id}>
                <div>
                  <strong>{entry.shop_name || entry.seller_name} · TZS {formatMoney(entry.amount)}</strong>
                  <span><span>{entry.payment_method}</span> · {entry.payment_reference} · {entry.created_at}</span>
                  <small>{entry.recorded_by_name || "Admin"}{entry.note && ` · ${entry.note}`}</small>
                </div>
              </article>
            ))}
            {!payoutData.entries.length && <p className="empty-state">Hakuna malipo yaliyorekodiwa.</p>}
          </div>
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

      {tab === "coupons" && isAdmin && (
        <section className="dashboard-content">
          <form className="form-card inline-feature-form" onSubmit={createCoupon}>
            <h2>Tengeneza kuponi ya majaribio</h2>
            <p className="field-hint">Kuponi zinatumika kwenye checkout ya demo pekee; hazipunguzi malipo halisi.</p>
            <label className="field-label" htmlFor="coupon-code">Msimbo wa kuponi</label>
            <input id="coupon-code" className="text-input" minLength={3} maxLength={32} required value={couponForm.code} onChange={(event) => setCouponForm({ ...couponForm, code: event.target.value.toUpperCase() })} />
            <label className="field-label" htmlFor="coupon-type">Aina ya punguzo</label>
            <select id="coupon-type" className="text-input" value={couponForm.discount_type} onChange={(event) => setCouponForm({ ...couponForm, discount_type: event.target.value })}>
              <option value="PERCENT">Asilimia (%)</option>
              <option value="FIXED">Kiasi maalum (TZS)</option>
            </select>
            <label className="field-label" htmlFor="coupon-value">Kiasi cha punguzo</label>
            <input id="coupon-value" className="text-input" type="number" min="1" max={couponForm.discount_type === "PERCENT" ? "100" : "999999999"} required value={couponForm.discount_value} onChange={(event) => setCouponForm({ ...couponForm, discount_value: event.target.value })} />
            <label className="field-label" htmlFor="coupon-limit">Idadi ya matumizi (hiari)</label>
            <input id="coupon-limit" className="text-input" type="number" min="1" value={couponForm.max_redemptions} onChange={(event) => setCouponForm({ ...couponForm, max_redemptions: event.target.value })} />
            <button className="generate-button" type="submit" disabled={busy}>Hifadhi kuponi</button>
          </form>
          <div className="feature-list">
            {coupons.map((coupon) => (
              <article className="seller-card" key={coupon.id}>
                <div>
                  <strong>{coupon.code}</strong>
                  <span>{coupon.discount_type === "PERCENT" ? `${coupon.discount_value}%` : `TZS ${formatMoney(coupon.discount_value)}`} · {translate("Matumizi ya kuponi", language)} {coupon.redemption_count}{coupon.max_redemptions ? ` / ${coupon.max_redemptions}` : ""}</span>
                  <small>{coupon.is_active ? "INAENDELEA" : "IMEZIMWA"} · Demo pekee</small>
                </div>
                <button className="table-action" type="button" onClick={() => changeCouponStatus(coupon, !coupon.is_active)}>
                  {coupon.is_active ? "Zima kuponi" : "Washa kuponi"}
                </button>
              </article>
            ))}
            {!coupons.length && <p className="empty-state">Hakuna kuponi zilizoundwa.</p>}
          </div>
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

      {tab === "returns" && isAdmin && (
        <section className="dashboard-content feature-list">
          <h2>Maombi ya kurejesha bidhaa/refund</h2>
          {returnRequests.map((item) => <article className="seller-card" key={item.id}><div><strong>{item.order_id} · {item.buyer_name}</strong><span>{item.reason} · TZS {formatMoney(item.amount)}</span><small><span>{item.status}</span> · <span>{item.payment_status}</span> · <span>{item.delivery_status}</span></small>{item.admin_note && <small>{item.admin_note}</small>}</div>{item.status === "PENDING" && <div className="seller-actions"><button type="button" onClick={() => resolveReturnRequest(item.id, "APPROVED")}>Idhinisha</button><button type="button" className="danger-action" onClick={() => resolveReturnRequest(item.id, "REJECTED")}>Kataa</button></div>}</article>)}
          {!returnRequests.length && <p className="empty-state">Hakuna ombi la marejesho.</p>}
          <p className="field-hint">Uidhinishaji ni uamuzi tu; mfumo hautoi pesa wala kubadilisha hali ya malipo. Refund halisi ishughulikiwe nje ya mfumo.</p>
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

      {tab === "security" && isAdmin && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading">
            <h2>Security Intelligence</h2>
            <button type="button" onClick={runSecurityScan}>Fanya scan sasa</button>
          </div>
          <p className="field-hint">
            Rule-based signals hutokana na rekodi halisi. OpenAI Responses huchambua tu
            counts zilizochujwa ikiwa OPENAI_API_KEY imewekwa; majina, barua pepe, IP na
            hatua za akaunti hazitumwi kwa model. Alert ni kiashiria cha ukaguzi, si uthibitisho.
          </p>
          <div className="security-filters">
            <label className="field-label">
              Hali
              <select className="text-input" value={securityFilters.status} onChange={(event) => setSecurityFilters({ ...securityFilters, status: event.target.value })}>
                <option value="">Hali zote</option>
                {["OPEN", "IN_REVIEW", "ACCOUNT_REVIEW", "WARNED", "SUSPENDED", "APPEAL_PENDING", "FALSE_POSITIVE", "RESOLVED"].map((status) => <option key={status} value={status}>{translate(status, language)}</option>)}
              </select>
            </label>
            <label className="field-label">
              Kiwango cha hatari
              <select className="text-input" value={securityFilters.severity} onChange={(event) => setSecurityFilters({ ...securityFilters, severity: event.target.value })}>
                <option value="">Viwango vyote</option>
                {["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((severity) => <option key={severity} value={severity}>{translate(severity, language)}</option>)}
              </select>
            </label>
            <label className="field-label">
              Aina ya alert
              <select className="text-input" value={securityFilters.category} onChange={(event) => setSecurityFilters({ ...securityFilters, category: event.target.value })}>
                <option value="">Aina zote</option>
                {["BRUTE_FORCE", "UNUSUAL_LOGIN_ACTIVITY", "SELLER_RISK", "REPEATED_CANCELLATIONS"].map((category) => <option key={category} value={category}>{translate(category.replaceAll("_", " "), language)}</option>)}
              </select>
            </label>
            <button type="button" className="table-action" onClick={refreshSecurityData}>Tumia vichujio</button>
          </div>
          <div className="security-alert-list">
            {securityAlerts.map((alert) => (
              <button
                type="button"
                className={`security-alert-card ${securityAlertDetail?.id === alert.id ? "selected" : ""}`}
                key={alert.id}
                onClick={() => openSecurityAlert(alert.id)}
              >
                <span className={`seller-status ${alert.severity === "CRITICAL" || alert.severity === "HIGH" ? "rejected" : "pending"}`}>{alert.severity} · rules {alert.risk_score}/100{alert.ai_used ? ` · AI ${alert.ai_risk_score}/100` : ""}</span>
                <strong>{alert.category.replaceAll("_", " ")}</strong>
                <small>{alert.subject_type}: {alert.subject_id} · {alert.status}</small>
                <small>{alert.occurred_at} · {alert.detection_model}{alert.ai_used ? " · AI" : " · rules"}</small>
              </button>
            ))}
            {!securityAlerts.length && <p className="empty-state">Hakuna alerts kwa vichujio hivi. Endesha scan kupata tathmini mpya.</p>}
          </div>
          {securityAlertDetail && (
            <article className="security-detail">
              <div className="dashboard-section-heading">
                <h3>Ushahidi na historia ya alert</h3>
                <button type="button" onClick={() => setSecurityAlertDetail(null)}>Funga</button>
              </div>
              <p><strong>{securityAlertDetail.subject_type}:</strong> {securityAlertDetail.subject_id} · {securityAlertDetail.category} · {securityAlertDetail.status}</p>
              <h4>Sababu za alama</h4>
              <ul>{securityAlertDetail.reasons.map((reason, index) => <li key={`${index}-${reason}`}>{reason}</li>)}</ul>
              <h4>Uchambuzi wa OpenAI</h4>
              {securityAlertDetail.ai_used ? (
                <>
                  <p>AI risk score: {securityAlertDetail.ai_risk_score}/100 · {securityAlertDetail.ai_model} · {securityAlertDetail.ai_analyzed_at}</p>
                  <ul>{securityAlertDetail.ai_reasons.map((reason, index) => <li key={`${index}-${reason}`}>{reason}</li>)}</ul>
                </>
              ) : <p className="field-hint">Bado hakuna uchambuzi wa AI uliothibitishwa kwa alert hii.</p>}
              <h4>Ushahidi uliorekodiwa</h4>
              <pre>{JSON.stringify(securityAlertDetail.evidence, null, 2)}</pre>
              <h4>Audit trail</h4>
              <ul className="security-history">
                {securityAlertDetail.actions.map((action) => (
                  <li key={action.id}>{action.action} · {action.actor_name || "Mfumo"} · {action.created_at}<small>{action.note}</small></li>
                ))}
              </ul>
              <label className="field-label" htmlFor="security-action-note">Sababu ya hatua ya admin</label>
              <textarea id="security-action-note" className="text-input security-note" value={securityActionNote} onChange={(event) => setSecurityActionNote(event.target.value)} maxLength={2000} />
              <div className="seller-actions">
                <button type="button" onClick={() => actOnSecurityAlert("WARN")}>Onya</button>
                <button type="button" onClick={() => actOnSecurityAlert("ACCOUNT_REVIEW")}>Weka kwenye review</button>
                {["ACCOUNT", "SELLER", "BUYER"].includes(securityAlertDetail.subject_type) && securityAlertDetail.status !== "SUSPENDED" && <button type="button" className="danger-action" onClick={() => actOnSecurityAlert("SUSPEND")}>Simamisha akaunti</button>}
                {securityAlertDetail.status === "SUSPENDED" && <button type="button" onClick={() => actOnSecurityAlert("RESTORE")}>Rejesha akaunti</button>}
                <button type="button" onClick={() => actOnSecurityAlert("FALSE_POSITIVE")}>Weka false positive</button>
                <button type="button" onClick={() => actOnSecurityAlert("RESOLVE")}>Tatua alert</button>
              </div>
            </article>
          )}
          <div className="dashboard-section-heading"><h3>Appeals zinazosubiri</h3><span>{securityAppeals.length}</span></div>
          <label className="field-label" htmlFor="security-appeal-note">Sababu ya uamuzi wa appeal</label>
          <textarea id="security-appeal-note" className="text-input security-note" value={securityAppealNote} onChange={(event) => setSecurityAppealNote(event.target.value)} maxLength={2000} />
          <div className="seller-list feature-list">
            {securityAppeals.map((appeal) => (
              <article className="seller-card security-appeal" key={appeal.id}>
                <div>
                  <strong>{appeal.appellant_name} · {appeal.appellant_email}</strong>
                  <span>{appeal.subject_type}: {appeal.subject_id} · {appeal.category} · {appeal.severity} ({appeal.risk_score}/100)</span>
                  <small>{appeal.message} · {appeal.created_at}</small>
                </div>
                <div className="seller-actions">
                  <button type="button" onClick={() => reviewSecurityAppeal(appeal.id, "ACCEPTED")}>Kubali na fungua upya ukaguzi</button>
                  <button type="button" className="danger-action" onClick={() => reviewSecurityAppeal(appeal.id, "REJECTED")}>Kataa appeal</button>
                </div>
              </article>
            ))}
            {!securityAppeals.length && <p className="empty-state">Hakuna appeal inayosubiri.</p>}
          </div>
        </section>
      )}

      {tab === "security" && !isAdmin && (
        <section className="dashboard-content feature-list">
          <div className="dashboard-section-heading">
            <h2>Tahadhari za usalama kwenye akaunti yako</h2>
            <span>{mySecurityAlerts.length} taarifa</span>
          </div>
          <p className="field-hint">Tahadhari ni viashiria vinavyokaguliwa na mtu; hazimaanishi moja kwa moja kuwa umefanya kosa.</p>
          {mySecurityAlerts.map((alert) => (
            <article className="security-detail" key={alert.id}>
              <div className="dashboard-section-heading">
                <h3>{alert.category.replaceAll("_", " ")}</h3>
                <span>{alert.severity} · {alert.risk_score}/100 · {alert.status}</span>
              </div>
              <ul>{alert.reasons.map((reason, index) => <li key={`${index}-${reason}`}>{reason}</li>)}</ul>
              <small>{alert.occurred_at} · {alert.detection_model}</small>
              {alert.appeal_status === "PENDING" ? (
                <p className="field-hint">Appeal yako inasubiri ukaguzi.</p>
              ) : (
                <>
                  {alert.appeal_status && <p className="field-hint">Hali ya appeal ya awali: {alert.appeal_status}</p>}
                  <label className="field-label" htmlFor={`appeal-${alert.id}`}>Eleza kwa nini unaomba ukaguzi upya</label>
                  <textarea
                    id={`appeal-${alert.id}`}
                    className="text-input security-note"
                    value={appealDrafts[alert.id] || ""}
                    onChange={(event) => setAppealDrafts({ ...appealDrafts, [alert.id]: event.target.value })}
                    maxLength={2000}
                  />
                  <button type="button" className="table-action" onClick={() => submitSecurityAppeal(alert.id)}>Tuma appeal</button>
                </>
              )}
            </article>
          ))}
          {!mySecurityAlerts.length && <p className="empty-state">Hakuna tahadhari za usalama kwenye akaunti yako.</p>}
        </section>
      )}

      {tab === "documents" && isAdmin && (
        <section className="dashboard-content feature-list">
          <div className="dashboard-section-heading"><h2>Nyaraka za wauzaji</h2><span>{adminDocuments.length} jumla</span></div>
          {adminDocuments.map((item) => (
            <article className="seller-card" key={item.id}>
              <div><strong>{item.filename}</strong><span>{item.seller_name} · {item.shop_name || item.seller_id}</span><small>{item.kind} · {item.content_type} · {item.created_at}</small></div>
              <a className="table-action" href={`/api/documents/${encodeURIComponent(item.id)}`} target="_blank" rel="noreferrer">Fungua salama</a>
            </article>
          ))}
          {!adminDocuments.length && <p className="empty-state">Hakuna nyaraka zilizopakiwa.</p>}
        </section>
      )}

      {tab === "notifications" && isAdmin && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading"><h2>Mipangilio ya notification providers</h2></div>
          {notificationSettings ? (
            <div className="seller-list feature-list">
              {Object.entries(notificationSettings).filter(([key]) => key !== "queue").map(([key, value]) => (
                <article className="seller-card" key={key}>
                  <div>
                    <strong>{key.replaceAll("_", " ").toUpperCase()}</strong>
                    <span>Provider: {value.provider || "Haijasajiliwa"}</span>
                    <small>{value.configured ? "Configured — provider credentials zinapatikana" : "Haijawekwa"}</small>
                  </div>
                  <span className={`seller-status ${value.configured ? "active" : "pending"}`}>{value.configured ? "READY" : "NOT CONFIGURED"}</span>
                </article>
              ))}
              <article className="seller-card">
                <div><strong>NOTIFICATION QUEUE</strong><span>Provider abstraction: EMAIL/SMS/WEB_PUSH/MOBILE_PUSH/WHATSAPP</span><small>{notificationSettings.queue.service_configured ? "Service URL imewekwa" : "NOTIFICATION_SERVICE_URL haijawekwa"}</small></div>
                <span className={`seller-status ${notificationSettings.queue.service_configured ? "active" : "pending"}`}>{notificationSettings.queue.service_configured ? "READY" : "NOT CONFIGURED"}</span>
              </article>
            </div>
          ) : <p className="empty-state">Mipangilio ya arifa haijapakiwa.</p>}
        </section>
      )}

      {tab === "health" && isAdmin && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading"><h2>Afya na ufuatiliaji wa mfumo</h2><button type="button" onClick={refreshSystemHealth}>Sasisha</button></div>
          {systemHealth ? (
            <>
              <div className="stats-grid">
                <StatCard title="Hali ya mfumo" value={systemHealth.status} />
                <StatCard title="Database" value={systemHealth.database} />
                <StatCard title="Redis" value={systemHealth.redis} />
                <StatCard title="Errors za saa moja" value={systemHealth.server_errors_last_hour} />
                <StatCard title="Uptime (sekunde)" value={systemHealth.uptime_seconds} />
                <StatCard title="Backups za S3" value={systemHealth.backups.configured ? "Zimesanidiwa" : "Hazijasanidiwa"} note={systemHealth.backups.last_success_at || "Backup ya kwanza bado haijafanyika."} />
              </div>
              {!systemHealth.backups.configured && <p className="form-error dashboard-alert" role="alert">Backups hazijawashwa. Weka mipangilio ya S3 kwenye mazingira ya server.</p>}
              <p className="field-hint">Makosa ya seva yanaandikwa kwenye log za server; kipimo hiki huhesabu HTTP 5xx katika mchakato huu.</p>
            </>
          ) : <p className="empty-state">Taarifa za afya hazipatikani.</p>}
        </section>
      )}

      {tab === "kyc" && (
        <section className="dashboard-content">
          {isAdmin ? (
            <>
              <div className="dashboard-section-heading"><h2>Ukaguzi wa KYC</h2><span>{sellerKycRows.length} maombi</span></div>
              <div className="seller-list feature-list">
                {sellerKycRows.map((row) => (
                  <article className="seller-card" key={row.seller_id}>
                    <div>
                      <strong>{row.shop_name || row.full_name}</strong>
                      <span>{row.full_name} · {row.email}</span>
                      <small>{row.seller_type} · {row.status} · {row.updated_at}</small>
                      {row.rejection_reason && <small>Sababu: {row.rejection_reason}</small>}
                    </div>
                    <div className="seller-actions">
                      <button type="button" onClick={() => reviewKyc(row.seller_id, "UNDER_REVIEW", "")}>Anza ukaguzi</button>
                      <button type="button" onClick={() => reviewKyc(row.seller_id, "VERIFIED", "")}>Approve</button>
                      <button type="button" className="danger-action" onClick={() => reviewKyc(row.seller_id, "RESUBMISSION_REQUIRED", "Tafadhali wasilisha nyaraka sahihi au zilizokamilika.")}>Omba nyaraka nyingine</button>
                      <button type="button" className="danger-action" onClick={() => reviewKyc(row.seller_id, "REJECTED", "Taarifa au nyaraka hazikupitisha uthibitisho.")}>Reject</button>
                    </div>
                  </article>
                ))}
                {!sellerKycRows.length && <p className="empty-state">Hakuna maombi ya KYC bado.</p>}
              </div>
            </>
          ) : (
            <>
              <div className="dashboard-section-heading">
                <h2>KYC ya muuzaji</h2>
                <span>{sellerKyc?.status || "NOT_VERIFIED"}</span>
              </div>
              {sellerKyc?.status === "VERIFIED" ? (
                <p className="success-message">Muuzaji ametathminishwa Verified.</p>
              ) : (
                <form className="form-card" onSubmit={submitKyc}>
                  <label className="field-label" htmlFor="kyc-seller-type">Aina ya seller</label>
                  <select id="kyc-seller-type" className="text-input" value={kycForm.seller_type} onChange={(event) => setKycForm({ ...kycForm, seller_type: event.target.value })}>
                    <option value="INDIVIDUAL">{translate("Individual Seller", language)}</option>
                    <option value="BUSINESS">{translate("Business Seller", language)}</option>
                  </select>
                  <label className="field-label" htmlFor="kyc-phone">Namba ya simu</label>
                  <input id="kyc-phone" className="text-input" required value={kycForm.phone} onChange={(event) => setKycForm({ ...kycForm, phone: event.target.value })} />
                  {kycForm.seller_type === "INDIVIDUAL" ? (
                    <>
                      <label className="field-label" htmlFor="kyc-name">Majina ya utambulisho</label>
                      <input id="kyc-name" className="text-input" required value={kycForm.identity_name} onChange={(event) => setKycForm({ ...kycForm, identity_name: event.target.value })} />
                      <label className="field-label" htmlFor="kyc-number">Namba ya kitambulisho</label>
                      <input id="kyc-number" className="text-input" required value={kycForm.identity_number} onChange={(event) => setKycForm({ ...kycForm, identity_number: event.target.value })} />
                    </>
                  ) : (
                    <>
                      <label className="field-label" htmlFor="kyc-business-name">Jina la biashara</label>
                      <input id="kyc-business-name" className="text-input" required value={kycForm.business_name} onChange={(event) => setKycForm({ ...kycForm, business_name: event.target.value })} />
                      <label className="field-label" htmlFor="kyc-registration">Namba ya usajili</label>
                      <input id="kyc-registration" className="text-input" value={kycForm.business_registration_number} onChange={(event) => setKycForm({ ...kycForm, business_registration_number: event.target.value })} />
                      <label className="field-label" htmlFor="kyc-tin">TIN</label>
                      <input id="kyc-tin" className="text-input" value={kycForm.tin_number} onChange={(event) => setKycForm({ ...kycForm, tin_number: event.target.value })} />
                      <label className="field-label" htmlFor="kyc-license">Leseni ya biashara</label>
                      <input id="kyc-license" className="text-input" value={kycForm.business_license_number} onChange={(event) => setKycForm({ ...kycForm, business_license_number: event.target.value })} />
                      <label className="field-label" htmlFor="kyc-address">Anwani ya biashara</label>
                      <input id="kyc-address" className="text-input" required value={kycForm.business_address} onChange={(event) => setKycForm({ ...kycForm, business_address: event.target.value })} />
                    </>
                  )}
                  <button className="generate-button" type="submit">Tuma ombi la KYC</button>
                </form>
              )}
              {sellerKyc?.status !== "VERIFIED" && (
                <form className="form-card" onSubmit={uploadDocument}>
                  <h2>Pakia nyaraka ya KYC</h2>
                  <input type="hidden" name="document-kind" value="seller_verification" />
                  <label className="field-label" htmlFor="kyc-document">PDF, JPEG, PNG au WebP; hadi MB 10</label>
                  <input id="kyc-document" className="text-input" name="document" type="file" accept="application/pdf,image/jpeg,image/png,image/webp" required />
                  <button className="generate-button" type="submit">Pakia nyaraka salama</button>
                </form>
              )}
              {sellerKyc?.documents?.length > 0 && (
                <div className="feature-list">
                  <h2>Nyaraka zilizowasilishwa</h2>
                  {sellerKyc.documents.map((document) => (
                    <article className="seller-card" key={document.id}>
                      <div><strong>{document.filename}</strong><small>{document.document_role || "KYC"} · {document.created_at}</small></div>
                      <a className="table-action" href={`/api/documents/${encodeURIComponent(document.id)}`} target="_blank" rel="noreferrer">Fungua</a>
                    </article>
                  ))}
                </div>
              )}
              {sellerKyc?.status && !["VERIFIED"].includes(sellerKyc.status) && (
                <button className="danger-action" type="button" onClick={deleteKyc}>Futa ombi na nyaraka za KYC</button>
              )}
            </>
          )}
        </section>
      )}

      {tab === "business" && !isAdmin && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading"><h2>Biashara na matawi</h2></div>
          <form className="form-card" onSubmit={createBusiness}>
            <h2>Unda biashara mpya</h2>
            <label className="field-label" htmlFor="business-name">Jina la biashara</label>
            <input id="business-name" className="text-input" required value={businessForm.name} onChange={(event) => setBusinessForm({ ...businessForm, name: event.target.value })} />
            <label className="field-label" htmlFor="business-type">Aina ya biashara</label>
            <input id="business-type" className="text-input" required value={businessForm.business_type} onChange={(event) => setBusinessForm({ ...businessForm, business_type: event.target.value })} />
            <label className="field-label" htmlFor="business-address">Anwani</label>
            <input id="business-address" className="text-input" value={businessForm.address} onChange={(event) => setBusinessForm({ ...businessForm, address: event.target.value })} />
            <label className="field-label" htmlFor="business-phone">Simu</label>
            <input id="business-phone" className="text-input" value={businessForm.phone} onChange={(event) => setBusinessForm({ ...businessForm, phone: event.target.value })} />
            <label className="field-label" htmlFor="business-email">Barua pepe</label>
            <input id="business-email" className="text-input" type="email" value={businessForm.email} onChange={(event) => setBusinessForm({ ...businessForm, email: event.target.value })} />
            <button className="generate-button" type="submit">Unda biashara</button>
          </form>

          {businesses.length > 0 && (
            <div className="feature-columns">
              <section className="form-card">
                <h2>Chagua biashara</h2>
                <select className="text-input" value={selectedBusinessId} onChange={(event) => loadBusinessSummary(event.target.value)}>
                  {businesses.map((business) => (
                    <option key={business.id} value={business.id}>{business.name}</option>
                  ))}
                </select>
                {businessSummary && (
                  <div className="feature-list">
                    <h2>Ulinganisho wa matawi</h2>
                    {businessSummary.branches.map((branch) => (
                      <article className="seller-card" key={branch.branch_id}>
                        <div>
                          <strong>{branch.branch_name}</strong>
                          <span>Oda: {branch.order_count}</span>
                          <small>Mauzo: TZS {formatMoney(branch.gross_sales)} · Bidhaa hai: {branch.active_products} · Low stock: {branch.low_stock}</small>
                        </div>
                        <span className={`seller-status ${branch.is_active ? "active" : "pending"}`}>{branch.is_active ? "ACTIVE" : "INACTIVE"}</span>
                      </article>
                    ))}
                  </div>
                )}
              </section>
              <form className="form-card" onSubmit={createBranch}>
                <h2>Ongeza tawi</h2>
                <label className="field-label" htmlFor="branch-name">Jina la tawi</label>
                <input id="branch-name" className="text-input" required value={branchForm.name} onChange={(event) => setBranchForm({ ...branchForm, name: event.target.value })} />
                <label className="field-label" htmlFor="branch-address">Anwani</label>
                <input id="branch-address" className="text-input" value={branchForm.address} onChange={(event) => setBranchForm({ ...branchForm, address: event.target.value })} />
                <label className="field-label" htmlFor="branch-phone">Simu</label>
                <input id="branch-phone" className="text-input" value={branchForm.phone} onChange={(event) => setBranchForm({ ...branchForm, phone: event.target.value })} />
                <button className="generate-button" type="submit">Ongeza tawi</button>
              </form>
              <form className="form-card" onSubmit={createStaff}>
                <h2>Ongeza mfanyakazi au meneja</h2>
                <label className="field-label" htmlFor="staff-email">Barua pepe ya mtumiaji</label>
                <input id="staff-email" className="text-input" type="email" required value={staffForm.email} onChange={(event) => setStaffForm({ ...staffForm, email: event.target.value })} />
                <label className="field-label" htmlFor="staff-role">Ruhusa ya msingi</label>
                <select id="staff-role" className="text-input" value={staffForm.role} onChange={(event) => setStaffForm({ ...staffForm, role: event.target.value })}>
                  <option value="STAFF">{translate("Mfanyakazi wa tawi", language)}</option>
                  <option value="BRANCH_MANAGER">{translate("Meneja wa tawi", language)}</option>
                </select>
                <label className="field-label" htmlFor="staff-branch">Tawi</label>
                <select id="staff-branch" className="text-input" value={staffForm.branch_id} onChange={(event) => setStaffForm({ ...staffForm, branch_id: event.target.value })}>
                  <option value="">Kiteuzi cha tawi</option>
                  {businessSummary?.branches.map((branch) => (
                    <option key={branch.branch_id} value={branch.branch_id}>{branch.branch_name}</option>
                  ))}
                </select>
                <button className="generate-button" type="submit">Ongeza mwanachama</button>
              </form>
            </div>
          )}
        </section>
      )}

      {tab === "alerts" && isAdmin && (
        <section className="dashboard-content feature-list">
          <h2>{translate("Arifa za mfumo", language)}</h2>
          {systemAlerts.map((alert) => (
            <article className="seller-card" key={alert.id}>
              <div>
                <strong>{translate(alert.alert_type, language)}</strong>
                <span>{translate(alert.message, language)}</span>
                <small>{alert.created_at}</small>
              </div>
              {alert.acknowledged_at
                ? <small>{translate("Imeshughulikiwa", language)}</small>
                : <button className="table-action" type="button" onClick={() => acknowledgeSystemAlert(alert.id)}>{translate("Weka imeshughulikiwa", language)}</button>}
            </article>
          ))}
          {!systemAlerts.length && <p className="empty-state">{translate("Hakuna arifa za mfumo.", language)}</p>}
        </section>
      )}

      {tab === "policies" && isAdmin && (
        <section className="dashboard-content feature-list">
          <div className="demo-notice dashboard-notice">
            <p><strong>{translate("RASIMU — HAIJACHAPISHWA", language)}</strong> {translate("Kurasa hizi ni rasimu za ukaguzi wa admin pekee; hazijawekwa hadharani.", language)}</p>
            <p>{translate("Jaza jina la kisheria la biashara na mawasiliano ya msaada, kisha pata ukaguzi wa kisheria kabla ya kuchapisha.", language)}</p>
          </div>
          <article className="seller-card"><div><strong>{translate("Masharti ya matumizi", language)}</strong><ul>
            <li>{translate("Marketplace huunganisha wanunuzi na wauzaji; muuzaji anawajibika kwa maelezo na stock ya bidhaa.", language)}</li>
            <li>{translate("Bei ya delivery huwekwa tofauti na malipo ya bidhaa na huonyeshwa kwa mnunuzi.", language)}</li>
            <li>{translate("Demo mode haihamishi pesa; PIN au OTP haipaswi kuingizwa kwenye tovuti.", language)}</li>
          </ul></div></article>
          <article className="seller-card"><div><strong>{translate("Sera ya faragha", language)}</strong><ul>
            <li>{translate("Data inayokusanywa: jina, email, simu, anuani ya delivery, oda na ujumbe wa msaada.", language)}</li>
            <li>{translate("Data hutumika kuendesha akaunti, malipo, ufuatiliaji wa oda na msaada.", language)}</li>
            <li>{translate("Bainisha muda wa kuhifadhi data, ombi la nakala au kufuta data, na mawasiliano ya kampuni.", language)}</li>
          </ul></div></article>
          <article className="seller-card"><div><strong>{translate("Cancellation na refund", language)}</strong><ul>
            <li>{translate("Cancellation inaruhusiwa kabla oda haijasafirishwa.", language)}</li>
            <li>{translate("Ombi la return litumwe ndani ya siku 7 tangu delivery na litapitiwa na admin.", language)}</li>
            <li>{translate("Approval ya return ni uamuzi tu; mfumo hautoi refund ya pesa halisi moja kwa moja.", language)}</li>
          </ul></div></article>
          <article className="seller-card"><div><strong>{translate("Delivery na payout", language)}</strong><ul>
            <li>{translate("Muuzaji huingiza hatua za delivery, kampuni ya usafirishaji na tracking code kwa mikono.", language)}</li>
            <li>{translate("Payout ya Abliner inalengwa baada ya siku 7 tangu delivery bila dispute inayosubiri.", language)}</li>
            <li>{translate("Uhamisho wa payout haujawashwa; unahitaji KYC, salio la wallet, API na ratiba ya kuaminika.", language)}</li>
          </ul></div></article>
        </section>
      )}

      {tab === "settings" && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading"><h2>{translateKey("settings.title", language)}</h2></div>
          <section className="form-card settings-form background-settings" aria-labelledby="background-settings-title">
            <h2 id="background-settings-title">{translateKey("settings.background.title", language)}</h2>
            <p className="field-hint">{translateKey("settings.background.description", language)}</p>
            <div className="background-palette" role="group" aria-label={translateKey("settings.background.title", language)}>
              {backgroundThemes.map((theme) => {
                const label = translateKey(theme.labelKey, language);
                const selected = backgroundColor === theme.color;
                return (
                  <button
                    className={`background-choice${selected ? " selected" : ""}`}
                    type="button"
                    key={theme.color}
                    aria-label={translateKey("settings.background.select", language, { color: label })}
                    aria-pressed={selected}
                    onClick={() => setBackgroundColor(theme.color)}
                  >
                    <span className="background-swatch" style={{ backgroundColor: theme.color }} aria-hidden="true" />
                    <span>{label}</span>
                    {selected && <small>{translateKey("settings.background.selected", language)}</small>}
                  </button>
                );
              })}
            </div>
          </section>
          {!isAdmin && (
            <section className="form-card settings-form profile-image-settings" aria-labelledby="seller-profile-image-title">
              <h2 id="seller-profile-image-title">{translate("Picha ya profile ya muuzaji", language)}</h2>
              {user.profile_image_url && (
                <img className="profile-image-preview" src={user.profile_image_url} alt={user.shop_name || user.full_name} />
              )}
              <form className="buyer-form" onSubmit={uploadProfileImage}>
                <label className="field-label" htmlFor="seller-profile-image">{translate("Pakia picha ya profile (JPEG, PNG au WebP)", language)}</label>
                <input id="seller-profile-image" name="profile_image" className="text-input" type="file" accept="image/jpeg,image/png,image/webp" required />
                <button className="generate-button" type="submit">{translate("Hifadhi picha", language)}</button>
              </form>
            </section>
          )}
          <form className="form-card settings-form" onSubmit={changePassword}>
            <h2>{translateKey("settings.password", language)}</h2>
            <label className="field-label" htmlFor="current-password">Nenosiri la sasa</label>
            <input id="current-password" className="text-input" type="password" autoComplete="current-password" required value={passwordForm.current_password} onChange={(event) => setPasswordForm({ ...passwordForm, current_password: event.target.value })} />
            <label className="field-label" htmlFor="new-password">Nenosiri jipya (angalau herufi 10)</label>
            <input id="new-password" className="text-input" type="password" autoComplete="new-password" minLength={10} required value={passwordForm.new_password} onChange={(event) => setPasswordForm({ ...passwordForm, new_password: event.target.value })} />
            <button className="generate-button" type="submit">Hifadhi nenosiri</button>
          </form>
          <AccountSecurity />
          {!isAdmin && (
            <section className="form-card settings-form">
              <h2>Pakia nyaraka</h2>
              <form className="buyer-form" onSubmit={uploadDocument}>
                <label className="field-label" htmlFor="document-kind">Aina ya nyaraka</label>
                <select id="document-kind" className="text-input" value={documentKind} onChange={(event) => setDocumentKind(event.target.value)}>
                  <option value="seller_verification">Uthibitisho wa muuzaji</option>
                  <option value="product_document">Nyaraka za bidhaa</option>
                </select>
                {documentKind === "product_document" && (
                  <>
                    <label className="field-label" htmlFor="document-product">Bidhaa</label>
                    <select id="document-product" className="text-input" required value={documentProductId} onChange={(event) => setDocumentProductId(event.target.value)}>
                      <option value="">Chagua bidhaa</option>
                      {products.map((product) => <option key={product.id} value={product.id}>{product.name}</option>)}
                    </select>
                  </>
                )}
                <label className="field-label" htmlFor="account-document">Faili (PDF/JPEG/PNG/WebP; hadi MB 10)</label>
                <input id="account-document" className="text-input" name="document" type="file" accept="application/pdf,image/jpeg,image/png,image/webp" required />
                <button className="generate-button" type="submit">Pakia nyaraka</button>
              </form>
              <div className="feature-list">
                {documents.map((item) => <article className="seller-card" key={item.id}><div><strong>{item.filename}</strong><span>{item.kind} · {item.created_at}</span></div><a className="table-action" href={`/api/documents/${encodeURIComponent(item.id)}`}>Pakua</a></article>)}
                {!documents.length && <p className="empty-state">Bado hakuna nyaraka.</p>}
              </div>
            </section>
          )}
        </section>
      )}

        </div>
      </div>
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
  const { language } = useLanguage();
  const [deliveryStatus, setDeliveryStatus] = useState(sale.delivery_status || "UNFULFILLED");
  const [deliveryCarrier, setDeliveryCarrier] = useState(sale.delivery_carrier || "");
  const [trackingCode, setTrackingCode] = useState(sale.tracking_code || "");
  const [deliveryFee, setDeliveryFee] = useState(String(sale.delivery_fee || 0));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useFeedbackState("error");
  async function saveDelivery() {
    setBusy(true);
    setError("");
    try {
      await onUpdateDelivery(
        sale.order_id,
        deliveryStatus,
        deliveryCarrier,
        trackingCode,
        Number(deliveryFee),
      );
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }
  return localize(
    <tr className={sale.archived_at ? "archived-row" : ""}>
      <td><strong>{sale.product_name}</strong>{sale.variant_label && <small>{sale.variant_label}</small>}<small>{sale.order_id}</small></td>
      {isAdmin && <td>{sale.shop_name || "—"}</td>}
      <td>{sale.buyer_name || "—"}<small>{paymentMethodLabel(sale.payment_method || "")}</small>{sale.delivery_address && <small>{sale.delivery_address}</small>}</td>
      <td><span className={`order-status ${sale.payment_status.toLowerCase()}`}>{sale.payment_status === "COMPLETED" ? translate("Imekamilika (demo)", language) : translate(sale.payment_status, language)}</span>{sale.archived_at && <small>{translate("Imefichwa na muuzaji", language)}</small>}</td>
      <td>
        {sale.payment_status === "COMPLETED" && onUpdateDelivery ? (
          <div className="delivery-editor">
            <select className="text-input" aria-label="Hali ya delivery" value={deliveryStatus} onChange={(event) => setDeliveryStatus(event.target.value)}>
              {["UNFULFILLED", "PROCESSING", "ORDER_CONFIRMED", "SELLER_PREPARING", "READY_FOR_PICKUP", "AGENT_ASSIGNED", "PICKED_UP", "IN_TRANSIT", "NEAR_DESTINATION", "OUT_FOR_DELIVERY", "DELIVERED", "DELIVERY_FAILED", "RETURNED"].map((status) => (
                <option key={status} value={status}>{translate(status, language)}</option>
              ))}
            </select>
            <input className="text-input" aria-label="Jina la kampuni ya usafirishaji" maxLength={100} placeholder="Jina la kampuni ya usafirishaji" value={deliveryCarrier} onChange={(event) => setDeliveryCarrier(event.target.value)} />
            <input className="text-input" aria-label="Namba ya ufuatiliaji" maxLength={100} placeholder="Tracking code" value={trackingCode} onChange={(event) => setTrackingCode(event.target.value)} />
            <input className="text-input" aria-label="Ada ya delivery (TZS)" type="number" min="0" max="999999999" step="1" value={deliveryFee} onChange={(event) => setDeliveryFee(event.target.value)} />
            <small>Ada ya delivery inalipwa moja kwa moja kwa muuzaji; haijajumuishwa kwenye malipo ya bidhaa.</small>
            <button type="button" className="table-action" disabled={busy || deliveryStatus === "UNFULFILLED" || !Number.isInteger(Number(deliveryFee)) || Number(deliveryFee) < 0} onClick={saveDelivery}>{busy ? "..." : "Hifadhi"}</button>
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
        <select className="text-input" aria-label={translate("Hali ya ombi", language)} value={status} onChange={(event) => setStatus(event.target.value)}>
          {["OPEN", "IN_PROGRESS", "RESOLVED"].map((value) => <option key={value} value={value}>{translate(value, language)}</option>)}
        </select>
        <textarea className="text-input feature-textarea" aria-label="Jibu la admin" maxLength={4000} value={response} onChange={(event) => setResponse(event.target.value)} />
        <button className="table-action" type="submit" disabled={busy}>{busy ? "Inahifadhi..." : "Hifadhi jibu"}</button>
        {error && <small className="form-error">{error}</small>}
      </form>
    </article>
  );
}

function VideoShoppingCard({ product, video }) {
  const localize = useLocalize();
  const { language } = useLanguage();
  const { notify } = useNotification();
  const [variantId, setVariantId] = useState("");
  const [error, setError] = useState("");
  const [added, setAdded] = useState(false);
  const variants = product.variants || [];
  const selectedVariant = variants.find((variant) => variant.id === variantId);
  const hasAvailableVariant = variants.some((variant) => variant.stock_quantity > 0);
  const isOutOfStock = variants.length
    ? !hasAvailableVariant || Boolean(selectedVariant && selectedVariant.stock_quantity <= 0)
    : product.stock_quantity <= 0;

  function addToCart() {
    setError("");
    if (variants.length && !variantId) {
      setError(translate("Chagua aina ya bidhaa.", language));
      return;
    }
    const result = addProductToCart(product.id, variantId);
    if (!result.added) {
      const message = result.reason === "variant-conflict"
        ? "Kikapu kina variant moja kwa kila bidhaa; badilisha variant kwenye kikapu."
        : "Idadi ya bidhaa kwenye kikapu imefika kikomo.";
      setError(translate(message, language));
      return;
    }
    setAdded(true);
    notify(translate("Bidhaa imeongezwa kwenye kikapu.", language), "success");
  }

  return localize(
    <article className="video-shopping-card">
      <video
        className="video-shopping-player"
        src={video.url}
        controls
        preload="metadata"
        playsInline
        aria-label={`${translate("Video ya bidhaa", language)} ${product.name}`}
      />
      <div className="video-shopping-details">
        <small>{translate("Video ya bidhaa", language)}</small>
        <h3>{product.name}</h3>
        <strong>TZS {formatMoney(product.amount)}</strong>
        <span>{product.shop_name || "Duka"}</span>
        {variants.length > 0 && (
          <label>
            <span className="field-label">{translate("Aina ya bidhaa", language)}</span>
            <select
              className="text-input"
              value={variantId}
              onChange={(event) => setVariantId(event.target.value)}
            >
              <option value="">{translate("Chagua aina ya bidhaa", language)}</option>
              {variants.map((variant) => (
                <option
                  key={variant.id}
                  value={variant.id}
                  disabled={variant.stock_quantity <= 0}
                >
                  {variant.label} · {translate("Stock inayopatikana", language)}: {variant.stock_quantity}
                </option>
              ))}
            </select>
          </label>
        )}
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="video-shopping-actions">
          <button
            className="table-action"
            type="button"
            onClick={addToCart}
            disabled={isOutOfStock}
          >
            {translate("Ongeza kikapuni", language)}
          </button>
          <a className="generate-button" href={`/pay/${product.id}`}>
            {translate("Nunua sasa", language)}
          </a>
        </div>
        {added && <a className="video-cart-link" href="/cart">{translate("Fungua kikapu", language)}</a>}
      </div>
    </article>
  );
}

function ProductVideoFeed({ products }) {
  const localize = useLocalize();
  const videos = products.flatMap((product) => (
    (product.videos || []).map((video) => ({ product, video }))
  ));
  if (!videos.length) return null;
  return localize(
    <section className="video-shopping-section">
      <div className="video-shopping-heading">
        <div>
          <div className="eyebrow"><span /> LIPARAHISI VIDEO</div>
          <h2>{translate("Video za bidhaa")}</h2>
          <p>{translate("Tazama video, ongeza bidhaa kikapuni au anza kuinunua moja kwa moja.")}</p>
        </div>
      </div>
      <div className="video-shopping-grid">
        {videos.map(({ product, video }) => (
          <VideoShoppingCard
            key={`${product.id}-${video.id}`}
            product={product}
            video={video}
          />
        ))}
      </div>
    </section>
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
      <ProductVideoFeed products={products} />
      {products.length ? (
        <SellerProductSections products={products} showSave={false} />
      ) : <p className="empty-state">Hakuna bidhaa za kuonyesha kwa sasa.</p>}
      <BrandFooter><span key="copyright">© 2026 LipaRahisi</span><span key="payments">Malipo halisi hayajaunganishwa</span></BrandFooter>
    </main>
  );
}

function PublicTrackingPage({ trackingNumber }) {
  const [tracking, setTracking] = useState(null);
  const [error, setError] = useFeedbackState("error");
  const qrRef = useRef(null);
  const barcodeRef = useRef(null);

  useEffect(() => {
    api(`/tracking/${encodeURIComponent(trackingNumber)}`)
      .then(setTracking)
      .catch((requestError) => setError(requestError.message));
  }, [trackingNumber]);

  useEffect(() => {
    if (!tracking || !qrRef.current) return;
    const value = `${window.location.origin}/track/${encodeURIComponent(tracking.tracking_number)}`;
    QRCode.toCanvas(qrRef.current, value, { width: 140, margin: 1 }).catch(() => {});
    if (barcodeRef.current) {
      try {
        JsBarcode(barcodeRef.current, tracking.tracking_number, {
          format: "CODE128",
          width: 2,
          height: 70,
          displayValue: true,
          margin: 8,
        });
      } catch {
        // Some browsers can reject an invalid barcode value; tracking text remains visible.
      }
    }
  }, [tracking]);

  return (
    <main className="page-shell">
      <Header label="Ufuatiliaji wa oda" />
      {error && <p className="form-error dashboard-alert" role="alert">{error}</p>}
      {!tracking && !error && <p className="buyer-intro">Inatafuta mzigo...</p>}
      {tracking && (
        <section className="dashboard-content">
          <div className="dashboard-welcome">
            <div>
              <div className="eyebrow"><span /> LIVE DELIVERY TRACKING</div>
              <h1>{tracking.tracking_number}</h1>
              <p>{tracking.product.name} — {tracking.seller.shop_name}</p>
            </div>
            <div className="tracking-codes">
              <canvas ref={qrRef} aria-label="QR ya tracking" />
              <svg ref={barcodeRef} aria-label="Barcode ya tracking" />
            </div>
          </div>
          <div className="success-message">
            Hali: <strong>{tracking.order_status}</strong>
          </div>
          {tracking.live_location ? (
            <div className="tracking-map">
              <iframe
                title="Ramani ya eneo la mwisho la delivery"
                src={`https://www.openstreetmap.org/export/embed.html?bbox=${tracking.live_location.longitude - 0.02}%2C${tracking.live_location.latitude - 0.02}%2C${tracking.live_location.longitude + 0.02}%2C${tracking.live_location.latitude + 0.02}&marker=${tracking.live_location.latitude}%2C${tracking.live_location.longitude}`}
                loading="lazy"
              />
              <p className="field-hint">
                Ilisasishwa {tracking.live_location.created_at}
                {tracking.live_location.accuracy_m ? `, usahihi takribani m ${Math.round(tracking.live_location.accuracy_m)}` : ""}
              </p>
            </div>
          ) : (
            <p className="empty-state">Eneo la live halipo au GPS imesitishwa kwa delivery hii.</p>
          )}
          {tracking.carrier.name && (
            <div className="demo-notice">
              <p><strong>{tracking.carrier.name}</strong>: {tracking.carrier.tracking_number || "Tracking namba ya carrier haipo"} — {tracking.carrier.status || "Hali haijasasishwa"}</p>
            </div>
          )}
          <ol className="tracking-timeline">
            {tracking.timeline.map((event) => (
              <li key={`${event.status}-${event.created_at}`}>
                <strong>{event.status}</strong>
                <p>{event.description}</p>
                <small>{event.created_at}</small>
              </li>
            ))}
          </ol>
          {tracking.proof_of_delivery && (
            <div className="workspace">
              <h2>Uthibitisho wa kupokea</h2>
              <p>Aliyepokea: {tracking.proof_of_delivery.recipient_name}</p>
              <p>Tarehe: {tracking.proof_of_delivery.received_at}</p>
              {tracking.proof_of_delivery.image_url && <img src={tracking.proof_of_delivery.image_url} alt="Picha ya uthibitisho" />}
            </div>
          )}
        </section>
      )}
      <BrandFooter><a className="retry-link" href="/">Rudi marketplace</a></BrandFooter>
    </main>
  );
}


function DeliveryAgentDashboard({ user, onLogout }) {
  const { language } = useLanguage();
  const localize = useLocalize();
  const [deliveries, setDeliveries] = useState([]);
  const [availableDeliveries, setAvailableDeliveries] = useState([]);
  const [sharing, setSharing] = useState(false);
  const [activeOrderId, setActiveOrderId] = useState("");
  const [lastPosition, setLastPosition] = useState(null);
  const [lastGpsAt, setLastGpsAt] = useState("");
  const [error, setError] = useFeedbackState("error");
  const [message, setMessage] = useFeedbackState("success");
  const watchRef = useRef(null);

  async function refresh() {
    const [assignedRows, availableRows] = await Promise.all([
      api("/deliveries"),
      api("/deliveries/available"),
    ]);
    setDeliveries(assignedRows);
    setAvailableDeliveries(availableRows);
  }

  useEffect(() => {
    refresh().catch((requestError) => setError(requestError.message));
  }, []);

  function stopSharing() {
    if (watchRef.current !== null && navigator.geolocation) {
      navigator.geolocation.clearWatch(watchRef.current);
      watchRef.current = null;
    }
    setSharing(false);
    setActiveOrderId("");
  }

  function startSharing(orderId) {
    if (!navigator.geolocation) {
      setError("GPS haipatikani kwenye kifaa hiki.");
      return;
    }
    if (watchRef.current !== null) stopSharing();
    setSharing(true);
    setActiveOrderId(orderId);
    watchRef.current = navigator.geolocation.watchPosition(
      (position) => {
        setLastPosition({
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
        });
        setLastGpsAt(new Date().toLocaleTimeString());
        api(`/orders/${encodeURIComponent(orderId)}/location`, {
          method: "POST",
          body: JSON.stringify({
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
            accuracy_m: position.coords.accuracy,
            sharing_consent: true,
          }),
        }).catch((requestError) => setError(requestError.message));
      },
      (positionError) => setError(positionError.message || "GPS imeshindikana kusasisha eneo."),
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 },
    );
  }

  async function updateStatus(orderId, status) {
    try {
      await api(`/orders/${encodeURIComponent(orderId)}/delivery`, {
        method: "PUT",
        body: JSON.stringify({ delivery_status: status, tracking_code: "", description: "Status updated by delivery agent" }),
      });
      setMessage("Hali ya delivery imesasishwa.");
      if (["DELIVERED", "DELIVERY_FAILED", "RETURNED"].includes(status)) stopSharing();
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function acceptDelivery(orderId) {
    try {
      await api(`/deliveries/${encodeURIComponent(orderId)}/accept`, { method: "POST" });
      setMessage("Delivery imekubaliwa na sasa imepangiwa akaunti yako.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  useEffect(() => () => stopSharing(), []);

  const activeStatuses = ["AGENT_ASSIGNED", "PICKED_UP", "IN_TRANSIT", "NEAR_DESTINATION", "OUT_FOR_DELIVERY"];
  const activeDeliveries = deliveries.filter((delivery) => activeStatuses.includes(delivery.delivery_status));
  const completedDeliveries = deliveries.filter((delivery) => ["DELIVERED", "DELIVERY_FAILED", "RETURNED"].includes(delivery.delivery_status));

  return localize(
    <main className="page-shell">
      <Header label="Delivery Agent" user={user} onLogout={onLogout} />
      <section className="dashboard-welcome">
        <div><div className="eyebrow"><span /> DELIVERY OPERATIONS</div><h1>Habari, <span>{user.full_name}</span></h1><p>Oda ulizopangiwa, deliveries zinazopatikana na GPS ya safari inayoendelea.</p></div>
      </section>
      {error && <p className="form-error dashboard-alert">{error}</p>}
      {message && <p className="success-message dashboard-alert">{message}</p>}
      <section className="dashboard-content">
        <div className="stats-grid">
          <StatCard title="Deliveries ulizopangiwa" value={deliveries.length} />
          <StatCard title="Safari zinazoendelea" value={activeDeliveries.length} />
          <StatCard title="Zinazopatikana" value={availableDeliveries.length} />
          <StatCard title="Zilizokamilika" value={completedDeliveries.filter((item) => item.delivery_status === "DELIVERED").length} />
          <StatCard title="Zilizoshindikana" value={completedDeliveries.filter((item) => item.delivery_status === "DELIVERY_FAILED").length} />
          <StatCard title="GPS" value={sharing ? "Inashare" : "Imesimama"} note={lastGpsAt ? `Ilisasishwa ${lastGpsAt}` : "GPS huanza baada ya kubonyeza Anzisha."} />
        </div>
        {sharing && lastPosition && (
          <section className="dashboard-panel delivery-live-map">
            <h2>Ramani ya eneo la agent</h2>
            <p>Ramani inaonyesha eneo la kifaa chako tu. Anwani za pickup na destination hazina coordinates zilizothibitishwa, hivyo route/ETA haijaonyeshwa.</p>
            <iframe
              title="Ramani ya eneo la delivery agent"
              src={`https://www.openstreetmap.org/export/embed.html?bbox=${lastPosition.longitude - 0.006}%2C${lastPosition.latitude - 0.006}%2C${lastPosition.longitude + 0.006}%2C${lastPosition.latitude + 0.006}&layer=mapnik&marker=${lastPosition.latitude}%2C${lastPosition.longitude}`}
              loading="lazy"
              referrerPolicy="no-referrer"
            />
            <small>Location imesasishwa {lastGpsAt || "..."}</small>
          </section>
        )}
        <div className="dashboard-grid">
          <section className="dashboard-panel">
            <h2>Deliveries ulizopangiwa</h2>
            <div className="seller-list feature-list">
              {deliveries.map((delivery) => (
                <article className="seller-card" key={delivery.order_id}>
                  <div>
                    <strong>{delivery.product_name}</strong>
                    <span>{delivery.order_id} · {delivery.delivery_status}</span>
                    {delivery.tracking_code && <small><a href={`/track/${encodeURIComponent(delivery.tracking_code)}`}>{delivery.tracking_code}</a></small>}
                    <small>{delivery.delivery_address || "Anuani ya destination haijapewa"}</small>
                  </div>
                  <div className="seller-actions">
                    {activeStatuses.includes(delivery.delivery_status) && (
                      <>
                        <button type="button" onClick={() => startSharing(delivery.order_id)}>Anzisha GPS live</button>
                        {sharing && activeOrderId === delivery.order_id && <button type="button" onClick={stopSharing}>Acha kushare GPS</button>}
                        {delivery.delivery_status === "AGENT_ASSIGNED" && <button type="button" onClick={() => updateStatus(delivery.order_id, "PICKED_UP")}>Thibitisha pickup</button>}
                        {["PICKED_UP", "AGENT_ASSIGNED"].includes(delivery.delivery_status) && <button type="button" onClick={() => updateStatus(delivery.order_id, "IN_TRANSIT")}>{translate("In transit", language)}</button>}
                        {["IN_TRANSIT", "NEAR_DESTINATION"].includes(delivery.delivery_status) && <button type="button" onClick={() => updateStatus(delivery.order_id, "OUT_FOR_DELIVERY")}>{translate("Out for delivery", language)}</button>}
                        {["PICKED_UP", "IN_TRANSIT", "NEAR_DESTINATION", "OUT_FOR_DELIVERY"].includes(delivery.delivery_status) && <button type="button" onClick={() => updateStatus(delivery.order_id, "DELIVERY_FAILED")}>{translate("Delivery failed", language)}</button>}
                      </>
                    )}
                    {delivery.delivery_status === "DELIVERY_FAILED" && <small>Imeshindikana · angalia historia/maelezo ya oda.</small>}
                  </div>
                </article>
              ))}
              {!deliveries.length && <p className="empty-state">Hakuna delivery uliyopewa kwa sasa.</p>}
            </div>
          </section>
          <section className="dashboard-panel">
            <h2>Deliveries zinazopatikana</h2>
            <p>Oda zilizoandaliwa na muuzaji na bado hazijapangiwa agent.</p>
            <div className="seller-list feature-list">
              {availableDeliveries.map((delivery) => (
                <article className="seller-card" key={delivery.order_id}>
                  <div>
                    <strong>{delivery.product_name}</strong>
                    <span>{delivery.shop_name || "Duka"} · {delivery.order_id}</span>
                    <small>{delivery.delivery_address || "Anuani ya destination haijapewa"}</small>
                  </div>
                  <button type="button" onClick={() => acceptDelivery(delivery.order_id)}>Kubali delivery</button>
                </article>
              ))}
              {!availableDeliveries.length && <p className="empty-state">Hakuna oda tayari kwa pickup kwa sasa.</p>}
            </div>
          </section>
        </div>
        <section className="dashboard-panel">
          <h2>Historia ya delivery</h2>
          <div className="dashboard-status-list">
            {completedDeliveries.map((delivery) => (
              <div className="dashboard-status-row" key={delivery.order_id}>
                <span>{delivery.product_name} · {delivery.order_id}</span>
                <strong>{translate(delivery.delivery_status, language)}</strong>
              </div>
            ))}
            {!completedDeliveries.length && <p className="empty-state">Bado hakuna delivery iliyokamilika au kushindikana.</p>}
          </div>
        </section>
      </section>
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
          <ProductVideoFeed products={storefront.products} />
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

function SellerProductSections({ products, onSave, showSave = true, maxPerPage = 4 }) {
  const { language } = useLanguage();
  const [pages, setPages] = useState({});
  const [detailProduct, setDetailProduct] = useState(null);
  const grouped = products.reduce((groups, product) => {
    const seller = product.shop_name || translate("Duka", language);
    if (!groups[seller]) groups[seller] = [];
    groups[seller].push(product);
    return groups;
  }, {});

  return (
    <div className="seller-product-sections">
      {Object.entries(grouped).map(([seller, sellerProducts]) => {
        const currentPage = pages[seller] || 0;
        const pageCount = Math.max(1, Math.ceil(sellerProducts.length / maxPerPage));
        const safePage = Math.min(currentPage, pageCount - 1);
        const visible = sellerProducts.slice(safePage * maxPerPage, (safePage + 1) * maxPerPage);
        return (
          <section className="seller-product-section" key={seller}>
            <div className="seller-product-heading">
              <div>
                <span>{translate("Muuzaji", language)}</span>
                <h2>{seller}</h2>
              </div>
              <span>{sellerProducts.length} {translate("Bidhaa", language)}</span>
            </div>
            <div className="seller-product-row">
              {visible.map((product) => (
                <article className="market-product seller-product-card" key={product.id}>
                  <ProductGallery product={product} className="market-product-gallery" />
                  <div>
                    <small>{product.category || translate("Bidhaa", language)}</small>
                    <h2>{product.name}</h2>
                    <p>{product.description || ""}</p>
                    <strong>TZS {formatMoney(product.amount, language)}</strong>
                  </div>
                  <div className="market-product-actions">
                    <button type="button" className="table-action" onClick={() => setDetailProduct(product)}>
                      {translate("Chunguza bidhaa", language)}
                    </button>
                    {showSave && (
                      <button type="button" className="table-action" onClick={() => onSave(product)}>
                        {translate("Hifadhi", language)}
                      </button>
                    )}
                    {product.store_slug && (
                      <a className="table-action" href={`/store/${encodeURIComponent(product.store_slug)}`}>
                        {translate("Tembelea duka", language)}
                      </a>
                    )}
                    <a className="generate-button" href={`/pay/${product.id}`}>
                      {translate("Nunua", language)}
                    </a>
                  </div>
                </article>
              ))}
            </div>
            {pageCount > 1 && (
              <div className="seller-pagination" aria-label={`${translate("Pages", language)} ${seller}`}>
                <button type="button" disabled={safePage === 0} onClick={() => setPages((current) => ({ ...current, [seller]: safePage - 1 }))}>
                  ‹
                </button>
                {Array.from({ length: pageCount }, (_, index) => (
                  <button
                    type="button"
                    key={index}
                    className={index === safePage ? "active" : ""}
                    aria-current={index === safePage ? "page" : undefined}
                    onClick={() => setPages((current) => ({ ...current, [seller]: index }))}
                  >
                    {index + 1}
                  </button>
                ))}
                <button type="button" disabled={safePage === pageCount - 1} onClick={() => setPages((current) => ({ ...current, [seller]: safePage + 1 }))}>
                  ›
                </button>
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

function BuyerDashboard({ user, onLogout }) {
  const localize = useLocalize();
  const { language, backgroundColor, setBackgroundColor } = useLanguage();
  const { notify } = useNotification();
  const [tab, setTab] = useState("overview");
  useEffect(() => {
    function openRequestedSettings() {
      if (window.location.hash === "#settings") setTab("account");
      else if (!window.location.hash) setTab("overview");
    }
    function openCustomSettings() {
      setTab("account");
    }
    openRequestedSettings();
    window.addEventListener("hashchange", openRequestedSettings);
    window.addEventListener("liparahisi-open-settings", openCustomSettings);
    return () => {
      window.removeEventListener("hashchange", openRequestedSettings);
      window.removeEventListener("liparahisi-open-settings", openCustomSettings);
    };
  }, []);
  const [products, setProducts] = useState([]);
  const [categories, setCategories] = useState([]);
  const [wishlist, setWishlist] = useState([]);
  const [orders, setOrders] = useState([]);
  const [tickets, setTickets] = useState([]);
  const [notificationHistory, setNotificationHistory] = useState([]);
  const [securityAlerts, setSecurityAlerts] = useState([]);
  const [cartCount, setCartCount] = useState(() => loadCart().reduce((sum, item) => sum + item.quantity, 0));
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [ticket, setTicket] = useState({ subject: "", message: "" });
  const [passwordForm, setPasswordForm] = useState({ current_password: "", new_password: "" });
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
    const [productRows, categoryRows, wishlistRows, orderRows, ticketRows, notificationRows, securityRows] = await Promise.all([
      api("/products"),
      api("/categories"),
      api("/wishlist"),
      api("/orders/history"),
      api("/support-tickets"),
      api("/notifications/history"),
      api("/security/alerts"),
    ]);
    setProducts(productRows);
    setCategories(categoryRows);
    setWishlist(wishlistRows);
    setOrders(orderRows);
    setTickets(ticketRows);
    setNotificationHistory(notificationRows);
    setSecurityAlerts(securityRows);
    setCartCount(loadCart().reduce((sum, item) => sum + item.quantity, 0));
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
      setMessage(translate("Nenosiri limebadilishwa.", language));
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  const filteredProducts = products.filter((product) =>
    (!category || product.category === category)
    && (!query || `${product.name} ${product.shop_name || ""}`.toLowerCase().includes(query.toLowerCase()))
  );
  const activeOrders = orders.filter((order) => order.payment_status === "COMPLETED"
    && !["DELIVERED", "DELIVERY_FAILED", "RETURNED", "CANCELLED"].includes(order.delivery_status));
  const wishlistCategories = new Set(wishlist.map((product) => product.category).filter(Boolean));
  const recommendedProducts = products
    .filter((product) => wishlistCategories.has(product.category)
      && !wishlist.some((saved) => saved.id === product.id))
    .slice(0, 4);
  const tabs = [["overview", "Muhtasari"], ["browse", "Bidhaa"], ["wishlist", "Vipendwa"], ["orders", "Oda zangu"], ["support", "Msaada"], ["account", "Akaunti"]];

  return localize(
    <main className="page-shell">
      <Header label="Akaunti ya mnunuzi" user={user} onLogout={onLogout} />
      <section className="dashboard-welcome"><div><div className="eyebrow"><span /> AKAUNTI YA MNUNUZI</div><h1>Habari, <span>{user.full_name}</span></h1><p>Vinjari bidhaa, fuatilia oda na wasiliana na timu ya msaada.</p></div><a className="table-action" href="/">Marketplace ya umma</a></section>
      <div className="dashboard-layout buyer-dashboard-layout">
        <aside className="dashboard-sidebar">
          <div className="dashboard-sidebar-heading">
            <span>Akaunti</span>
            <strong>{user.full_name}</strong>
          </div>
          <nav className="dashboard-tabs" aria-label="Sehemu za mnunuzi">
            {tabs.map(([key, title]) => <button key={key} type="button" className={tab === key ? "active" : ""} onClick={() => { setTab(key); setError(""); setMessage(""); if (window.location.hash === "#settings") window.history.replaceState(null, "", window.location.pathname + window.location.search); }}>{title}</button>)}
          </nav>
        </aside>
        <div className="dashboard-workspace">
          {error && <p className="form-error dashboard-alert" role="alert">{error}</p>}
          {message && <p className="success-message dashboard-alert" role="status">{message}</p>}
      {tab === "overview" && (
        <section className="dashboard-content">
          <div className="stats-grid">
            <StatCard title="Oda zote" value={orders.length} />
            <StatCard title="Oda zinazosafirishwa" value={activeOrders.length} />
            <StatCard title="Bidhaa ulizohifadhi" value={wishlist.length} />
            <StatCard title="Bidhaa kwenye kikapu" value={cartCount} />
            <StatCard title="Maombi ya msaada" value={tickets.length} />
            <StatCard title="Arifa zilizopokelewa" value={notificationHistory.length} />
            <StatCard title="Tahadhari za usalama" value={securityAlerts.length} />
          </div>
          <div className="dashboard-grid">
            <section className="dashboard-panel">
              <h2>Oda za karibuni</h2>
              {orders.slice(0, 5).map((order) => (
                <article className="dashboard-order-row" key={order.order_id}>
                  <div>
                    <strong>{order.product_name}</strong>
                    <span>{order.order_id} · {order.delivery_status || order.payment_status}</span>
                  </div>
                  {order.tracking_code
                    ? <a className="table-action" href={`/track/${encodeURIComponent(order.tracking_code)}`}>Fuatilia</a>
                    : <span>TZS {formatMoney(order.amount)}</span>}
                </article>
              ))}
              {!orders.length && <p className="empty-state">Bado huna oda zinazohusishwa na akaunti hii.</p>}
              <button type="button" className="table-action" onClick={() => setTab("orders")}>Oda zangu zote</button>
            </section>
            <section className="dashboard-panel">
              <h2>Bidhaa zinazoweza kukuvutia</h2>
              <p>Mapendekezo haya yanatokana na makundi ya bidhaa ulizohifadhi.</p>
              {recommendedProducts.map((product) => (
                <article className="dashboard-order-row" key={product.id}>
                  <div><strong>{product.name}</strong><span>{product.category} · TZS {formatMoney(product.amount)}</span></div>
                  <a className="table-action" href={`/pay/${product.id}`}>Nunua</a>
                </article>
              ))}
              {!recommendedProducts.length && <p className="empty-state">Hifadhi bidhaa ili tuweze kupendekeza bidhaa za makundi yanayokuvutia.</p>}
              <div className="dashboard-panel-actions">
                <button type="button" className="table-action" onClick={() => setTab("wishlist")}>Vipendwa</button>
                <a className="table-action" href="/cart">Kikapu ({cartCount})</a>
                <button type="button" className="table-action" onClick={() => setTab("account")}>Usalama wa akaunti</button>
              </div>
            </section>
          </div>
        </section>
      )}
      {tab === "browse" && (
        <section className="dashboard-content">
          <form className="product-search feature-search" onSubmit={(event) => event.preventDefault()}>
            <input className="text-input" type="search" placeholder="Tafuta bidhaa au duka..." value={query} onChange={(event) => setQuery(event.target.value)} />
            <select className="text-input" value={category} onChange={(event) => setCategory(event.target.value)}><option value="">Makundi yote</option>{categories.map((item) => <option key={item.id} value={item.name}>{item.name}</option>)}</select>
          </form>
          <ProductVideoFeed products={filteredProducts} />
          {filteredProducts.length ? (
            <SellerProductSections
              products={filteredProducts}
              onSave={(product) => runAction(
                () => api(`/wishlist/${product.id}`, { method: "POST" }),
                translate("Bidhaa imehifadhiwa kwenye vipendwa.", language),
              )}
            />
          ) : <p className="empty-state">{translate("Hakuna bidhaa zinazolingana na utafutaji.", language)}</p>}
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
      {tab === "account" && (
        <section className="dashboard-content buyer-settings-layout">
          <section className="form-card settings-form background-settings" aria-labelledby="buyer-background-settings-title">
            <h2 id="buyer-background-settings-title">{translateKey("settings.background.title", language)}</h2>
            <p className="field-hint">{translateKey("settings.background.description", language)}</p>
            <div className="background-palette" role="group" aria-label={translateKey("settings.background.title", language)}>
              {backgroundThemes.map((theme) => {
                const label = translateKey(theme.labelKey, language);
                const selected = backgroundColor === theme.color;
                return (
                  <button
                    className={`background-choice${selected ? " selected" : ""}`}
                    type="button"
                    key={theme.color}
                    aria-label={translateKey("settings.background.select", language, { color: label })}
                    aria-pressed={selected}
                    onClick={() => setBackgroundColor(theme.color)}
                  >
                    <span className="background-swatch" style={{ backgroundColor: theme.color }} aria-hidden="true" />
                    <span>{label}</span>
                    {selected && <small>{translateKey("settings.background.selected", language)}</small>}
                  </button>
                );
              })}
            </div>
          </section>
          <form className="form-card settings-form" onSubmit={changePassword}>
            <h2>{translateKey("settings.password", language)}</h2>
            <label className="field-label" htmlFor="buyer-current-password">{translate("Nenosiri la sasa", language)}</label>
            <input id="buyer-current-password" className="text-input" type="password" autoComplete="current-password" required value={passwordForm.current_password} onChange={(event) => setPasswordForm({ ...passwordForm, current_password: event.target.value })} />
            <label className="field-label" htmlFor="buyer-new-password">{translate("Nenosiri jipya (angalau herufi 10)", language)}</label>
            <input id="buyer-new-password" className="text-input" type="password" autoComplete="new-password" minLength={10} required value={passwordForm.new_password} onChange={(event) => setPasswordForm({ ...passwordForm, new_password: event.target.value })} />
            <button className="generate-button" type="submit">{translate("Hifadhi nenosiri", language)}</button>
          </form>
          <AccountSecurity />
        </section>
      )}
        </div>
      </div>
      <BrandFooter><span key="copyright">© 2026 LipaRahisi</span><span key="payments">Malipo ya majaribio pekee</span></BrandFooter>
    </main>
  );
}

function BuyerOrderRow({ order, onAction }) {
  const localize = useLocalize();
  const { language } = useLanguage();
  const [reason, setReason] = useState("");
  const [returnReason, setReturnReason] = useState("");
  const [rating, setRating] = useState("5");
  const [comment, setComment] = useState("");
  const [deliveryOtp, setDeliveryOtp] = useState("");
  const [recipientName, setRecipientName] = useState("");
  const cancellationInProgress = ["PENDING", "APPROVED"].includes(order.cancellation_status);
  const canCancel = ["PENDING", "COMPLETED"].includes(order.payment_status)
    && order.delivery_status !== "DELIVERED"
    && !order.return_status
    && !cancellationInProgress;
  const canRequestReturn = order.payment_status === "COMPLETED"
    && order.delivery_status === "DELIVERED"
    && !order.return_status
    && !cancellationInProgress;
  return localize(
    <tr>
      <td><strong>{order.order_id}</strong><small>{order.created_at}</small></td>
      <td>{order.product_name}{order.variant_label && <small>{order.variant_label}</small>}<small>{order.shop_name || "Muuzaji"}</small></td>
      <td>{translate(order.payment_status, language)}</td>
      <td>
        {translate(order.delivery_status || "UNFULFILLED", language)}
        {order.delivery_carrier && <small><span>Kampuni ya usafirishaji:</span> {order.delivery_carrier}</small>}
        {order.tracking_code && <small><span>Tracking code:</span> <a href={`/track/${encodeURIComponent(order.tracking_code)}`}>{order.tracking_code}</a></small>}
        {order.delivery_fee > 0 && (
          <small>
            <span>Ada ya delivery:</span> TZS {formatMoney(order.delivery_fee)}
            <br />
            Ada ya delivery inalipwa moja kwa moja kwa muuzaji; haijajumuishwa kwenye malipo ya bidhaa.
          </small>
        )}
        {order.return_status && (
          <small>
            <span>Hali ya ombi la kurejesha:</span> {order.return_status}
            {order.return_reason && ` · ${order.return_reason}`}
          </small>
        )}
        {order.delivery_address && <small>{order.delivery_address}</small>}
      </td>
      <td>TZS {formatMoney(order.amount)}</td>
      <td>
        <a className="table-action" href={`/pay/${order.product_id}?order_id=${encodeURIComponent(order.order_id)}`}>Fungua</a>
        {order.payment_status === "COMPLETED" && order.delivery_status !== "DELIVERED" && (
          <form className="inline-feature-form" onSubmit={(event) => {
            event.preventDefault();
            onAction(
              () => api(`/orders/${order.order_id}/confirm-delivery`, {
                method: "POST",
                body: JSON.stringify({ otp: deliveryOtp, recipient_name: recipientName }),
              }),
              "Delivery imeidhinishwa na umepokea mzigo.",
            );
          }}>
            <input aria-label="OTP ya delivery" className="text-input" minLength={4} maxLength={10} placeholder="OTP ya kupokea" required value={deliveryOtp} onChange={(event) => setDeliveryOtp(event.target.value)} />
            <input aria-label="Jina la aliyepokea" className="text-input" minLength={2} maxLength={100} placeholder="Jina la aliyepokea" required value={recipientName} onChange={(event) => setRecipientName(event.target.value)} />
            <button className="table-action" type="submit">Thibitisha nimepokea</button>
          </form>
        )}
        {canCancel && <form className="inline-feature-form" onSubmit={(event) => { event.preventDefault(); onAction(() => api(`/orders/${order.order_id}/cancellation`, { method: "POST", body: JSON.stringify({ reason }) }), "Ombi la kughairi limetumwa."); }}><input aria-label="Sababu ya kughairi" className="text-input" minLength={5} placeholder="Sababu ya kughairi" required value={reason} onChange={(event) => setReason(event.target.value)} /><button className="table-action" type="submit">Omba kughairi</button></form>}
        {canRequestReturn && <form className="inline-feature-form" onSubmit={(event) => { event.preventDefault(); onAction(() => api(`/orders/${order.order_id}/return`, { method: "POST", body: JSON.stringify({ reason: returnReason }) }), "Ombi la kurejesha limetumwa; hakuna refund itakayotekelezwa na mfumo."); }}><input aria-label="Sababu ya kurejesha" className="text-input" minLength={5} maxLength={1000} placeholder="Sababu ya kurejesha bidhaa/refund" required value={returnReason} onChange={(event) => setReturnReason(event.target.value)} /><button className="table-action" type="submit">Omba marejesho</button></form>}
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

function addProductToCart(productId, variantId = "") {
  const cart = loadCart();
  const existing = cart.find((item) => item.product_id === productId);
  if (existing && (existing.variant_id || "") !== variantId) {
    return { added: false, reason: "variant-conflict" };
  }
  if (existing && Number(existing.quantity) >= 99) {
    return { added: false, reason: "quantity-limit" };
  }
  if (existing) {
    existing.quantity = Number(existing.quantity) + 1;
  } else {
    cart.push({ product_id: productId, quantity: 1, variant_id: variantId });
  }
  saveCart(cart);
  return {
    added: true,
    count: cart.reduce((sum, item) => sum + Number(item.quantity), 0),
  };
}

function CheckoutPage({ productId }) {
  const localize = useLocalize();
  const { language } = useLanguage();
  const { notify } = useNotification();
  const [product, setProduct] = useState(null);
  const [paymentConfig, setPaymentConfig] = useState(null);
  const [form, setForm] = useState({ buyer_name: "", buyer_email: "", buyer_phone: "", delivery_address: "", payment_method: "M-Pesa", abandoned_checkout_consent: false });
  const [couponCode, setCouponCode] = useState("");
  const [variantId, setVariantId] = useState("");
  const [orderId, setOrderId] = useState(new URLSearchParams(window.location.search).get("order_id") || "");
  const [order, setOrder] = useState(null);
  const previousOrderStatus = useRef(null);
  const [error, setError] = useFeedbackState("error");
  const [busy, setBusy] = useState(false);
  const [cartCount, setCartCount] = useState(() => loadCart().reduce((sum, item) => sum + item.quantity, 0));

  useEffect(() => {
    setVariantId("");
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
    const hasVariants = (product?.variants || []).length > 0;
    if (hasVariants && !variantId) {
      setError(translate("Chagua aina ya bidhaa kabla ya kuongeza kwenye kikapu.", language));
      return;
    }
    const result = addProductToCart(productId, variantId || "");
    if (!result.added) {
      const message = result.reason === "variant-conflict"
        ? "Kikapu kina variant moja kwa kila bidhaa; badilisha variant kwenye kikapu."
        : "Idadi ya bidhaa kwenye kikapu imefika kikomo.";
      setError(translate(message, language));
      return;
    }
    setCartCount(result.count);
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
          coupon_code: couponCode,
          variant_id: variantId || null,
          abandoned_checkout_consent: form.abandoned_checkout_consent,
          request_key: newRequestKey(),
        }),
      });
      if (!result.order_id) throw new Error("Mfumo haukurudisha namba ya oda.");
      setOrderId(result.order_id);
      window.history.replaceState(
        {},
        "",
        `/pay/${productId}?order_id=${encodeURIComponent(result.order_id)}${result.sandbox ? "&sandbox=1" : result.demo ? "&demo=1" : ""}`,
      );
      if (result.payment_url) window.location.assign(result.payment_url);
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
      sandbox: order.sandbox,
      splits: [{
        shopName: product?.shop_name || "Muuzaji",
        amount,
        platformFee,
        sellerPayout: Number(order.seller_payout) || 0,
        items: [{
          product_name: order.variant_label
            ? `${order.product_name} — ${order.variant_label}`
            : order.product_name,
          quantity: 1,
          unit_amount: amount,
        }],
      }],
    }, language);
  }

  const isCompleted = order?.payment_status === "COMPLETED";
  const isLivePayment = paymentConfig?.live_payments_enabled === true;
  const isPesapalSandbox = paymentConfig?.pesapal_sandbox_enabled === true;
  const pesapalSandboxUnavailable =
    paymentConfig?.pesapal_sandbox_requested === true && !isPesapalSandbox;
  const isAblinerLive = paymentConfig?.abliner_live_enabled === true;
  const isAblinerSelected = form.payment_method === ABLINER_PAYMENT_METHOD;
  const paymentMethods = paymentOptionsFor(paymentConfig);
  const selectedMethod = networks.find((method) => method.value === form.payment_method);
  const productVariants = product?.variants || [];
  const selectedVariant = productVariants.find((variant) => variant.id === variantId);
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
            <ProductVideoFeed products={[product]} />
            <div className="checkout-amount">TZS {formatMoney(product.amount)}</div>
            <p className="buyer-intro">Jaza taarifa za oda na uchague njia ya malipo. PIN au OTP haiingizwe kwenye ukurasa huu.</p>
            <form className="buyer-form" onSubmit={submitCheckout}>
              {productVariants.length > 0 && (
                <>
                  <label className="field-label" htmlFor="checkout-variant">Aina ya bidhaa</label>
                  <select
                    id="checkout-variant"
                    className="text-input"
                    required
                    value={variantId}
                    onChange={(event) => setVariantId(event.target.value)}
                  >
                    <option value="">Chagua aina ya bidhaa</option>
                    {productVariants.map((variant) => (
                      <option key={variant.id} value={variant.id} disabled={variant.stock_quantity <= 0}>
                        {variant.label} · {translate("Stock inayopatikana", language)}: {variant.stock_quantity}
                      </option>
                    ))}
                  </select>
                </>
              )}
              <label className="field-label" htmlFor="buyer-name">Jina la mnunuzi</label>
              <input id="buyer-name" className="text-input" autoComplete="name" required minLength={2} value={form.buyer_name} onChange={(event) => setForm({ ...form, buyer_name: event.target.value })} />
              <label className="field-label" htmlFor="buyer-email">Barua pepe ya risiti</label>
              <input id="buyer-email" className="text-input" type="email" autoComplete="email" required value={form.buyer_email} onChange={(event) => setForm({ ...form, buyer_email: event.target.value })} />
              <label className="field-label" htmlFor="buyer-phone">Namba ya simu</label>
              <input id="buyer-phone" className="text-input" type="tel" inputMode="tel" autoComplete="tel" placeholder="Mf. 0712 345 678" required value={form.buyer_phone} onChange={(event) => setForm({ ...form, buyer_phone: event.target.value })} />
              <label className="field-label" htmlFor="buyer-address">Anuani ya delivery (hiari)</label>
              <input id="buyer-address" className="text-input" maxLength={500} autoComplete="street-address" value={form.delivery_address} onChange={(event) => setForm({ ...form, delivery_address: event.target.value })} />
              <label className="checkout-consent">
                <input
                  type="checkbox"
                  checked={form.abandoned_checkout_consent}
                  onChange={(event) => setForm({ ...form, abandoned_checkout_consent: event.target.checked })}
                />
                <span>{translate("Nataka kupokea ukumbusho mmoja wa checkout kupitia email baada ya saa 24 ikiwa sijalipa. Naweza kujiondoa kupitia link kwenye email.", language)}</span>
              </label>
              <label className="field-label" htmlFor="checkout-coupon">Msimbo wa kuponi (demo pekee)</label>
              <input id="checkout-coupon" className="text-input" maxLength={32} autoComplete="off" value={couponCode} onChange={(event) => setCouponCode(event.target.value.toUpperCase())} />
              {isAblinerSelected && (
                <p className="field-hint">Abliner itatuma ombi la malipo kwenye simu yako. Thibitisha kwenye simu; usiweke PIN au OTP kwenye tovuti hii.</p>
              )}
              {isLivePayment && !isAblinerSelected && (
                <p className="field-hint">ClickPesa itatuma ombi la USSD kwenye namba uliyoingiza. Ada za ClickPesa au mtandao zinaweza kutumika na zitaonyeshwa kwenye prompt. Thibitisha malipo kwenye simu yako; usiweke PIN au OTP kwenye tovuti hii.</p>
              )}
              {isPesapalSandbox && <p className="field-hint">Malipo yatafunguliwa kwenye PesaPal Sandbox. Hii ni ya majaribio tu; hakuna pesa halisi itakayokatwa.</p>}
              {pesapalSandboxUnavailable && <p className="form-error" role="alert">Mipangilio ya PesaPal Sandbox haijakamilika. Wasiliana na msimamizi wa mfumo.</p>}
              {!isPesapalSandbox && (!isLivePayment || isAblinerLive) && (
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
              {!isLivePayment && !isPesapalSandbox && form.payment_method.endsWith("Branch") && (
                <>
                  <label className="field-label" htmlFor="bank-branch">Tawi la benki</label>
                  <select id="bank-branch" className="text-input" required value={form.bank_branch} onChange={(event) => setForm({ ...form, bank_branch: event.target.value })}>
                    <option value="">Chagua tawi</option>
                    {bankBranches.map((branch) => <option key={branch} value={branch}>{branch}</option>)}
                  </select>
                  {form.bank_branch === "Tawi lingine" && (
                    <>
                      <label className="field-label" htmlFor="other-bank-branch">Andika jina la tawi</label>
                      <input id="other-bank-branch" className="text-input" required value={form.bank_branch_name} onChange={(event) => setForm({ ...form, bank_branch_name: event.target.value })} />
                    </>
                  )}
                </>
              )}
              {error && <p className="form-error" role="alert">{error}</p>}
              <button
                className="generate-button"
                type="submit"
                disabled={busy || pesapalSandboxUnavailable || (productVariants.length > 0 && (!selectedVariant || selectedVariant.stock_quantity <= 0))}
              >
                {busy ? "Inaandaa oda..." : `Endelea — TZS ${formatMoney(product.amount)}`}
              </button>
            </form>
            <button className="cart-secondary-button" type="button" onClick={addToCart} disabled={productVariants.length > 0 && (!selectedVariant || selectedVariant.stock_quantity <= 0)}>Ongeza bidhaa nyingine kwenye kikapu</button>
            {cartCount > 0 && <a className="cart-link" href="/cart">Nenda kikapu ({cartCount})</a>}
            <p className="checkout-footnote">{isAblinerSelected ? "Malipo yatakusanywa kupitia Abliner kwenye akaunti ya jukwaa; mgao wa muuzaji utalipwa kwa mikono." : isLivePayment ? "Malipo yatakusanywa na ClickPesa kwenye akaunti ya jukwaa; mgao wa muuzaji utalipwa kwa mikono." : "Huu ni mfumo wa majaribio: hakuna pesa inayokatwa au kuhamishwa kwa muuzaji."}</p>
          </section>
        )}
        {orderId && order && !isCompleted && order.payment_status !== "FAILED" && (
          <section className="checkout-card">
            <div className="eyebrow"><span /> {order.sandbox ? "PESAPAL SANDBOX" : order.demo ? "MALIPO YA MAJARIBIO" : order.payment_method === ABLINER_PAYMENT_METHOD ? "MALIPO YA ABLINER" : "MALIPO YA CLICKPESA"}</div>
            <h1>{order.demo ? "Thibitisha oda yako" : "Subiri ombi la malipo"}</h1>
            <div className="receipt-product">
              <span>{order.variant_label
                ? `${order.product_name || product?.name} — ${order.variant_label}`
                : order.product_name || product?.name}</span>
              <strong>TZS {formatMoney(order.amount)}</strong>
            </div>
            {order.coupon_discount > 0 && <p className="field-hint">{translate("Punguzo la kuponi", language)} ({order.coupon_code}): TZS {formatMoney(order.coupon_discount)}</p>}
            <div className="payment-result pending">
              <strong>Haijalipwa bado</strong>
              {order.sandbox ? (
                <span>Malipo ya PesaPal Sandbox yanathibitishwa. Hakuna pesa halisi inayohamishwa.</span>
              ) : order.demo ? (
                <span>Bonyeza kitufe chini kuthibitisha demo. Hakuna pesa halisi itakayohamishwa.</span>
              ) : order.payment_method === ABLINER_PAYMENT_METHOD ? (
                <span>Ombi la malipo la Abliner limetumwa kwa namba uliyoingiza. Thibitisha kwenye simu yako; usiweke PIN au OTP kwenye tovuti hii.</span>
              ) : (
                <span>Ombi la malipo limetumwa kwa namba uliyoingiza. Ada za ClickPesa au mtandao zinaweza kutumika na zitaonyeshwa kwenye prompt. Thibitisha kwenye simu yako; usiweke PIN au OTP kwenye tovuti hii.</span>
              )}
              <span>Njia ya malipo: {paymentMethodLabel(order.payment_method || (isAblinerSelected ? ABLINER_PAYMENT_METHOD : selectedMethod?.label))}{order.bank_branch ? ` — ${order.bank_branch}` : ""}.</span>
            </div>
            {error && <p className="form-error" role="alert">{error}</p>}
            {order.sandbox && order.payment_url && <a className="generate-button" href={order.payment_url}>Endelea na malipo ya sandbox</a>}
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
            <p className="receipt-kicker">{order.sandbox ? "RISITI YA PESAPAL SANDBOX" : order.demo ? "RISITI YA MAJARIBIO" : "RISITI YA MALIPO"}</p>
            <h1>Oda imekamilika</h1>
            <div className="receipt-product">
              <span>{order.variant_label
                ? `${order.product_name || product?.name} — ${order.variant_label}`
                : order.product_name || product?.name}</span>
              <strong>TZS {formatMoney(order.amount)}</strong>
            </div>
            <dl className="receipt-details">
              <div><dt>Namba ya oda</dt><dd>{order.order_id}</dd></div>
              <div><dt>Mnunuzi</dt><dd>{order.buyer_name || form.buyer_name || "Mgeni"}</dd></div>
              <div><dt>Njia ya malipo</dt><dd>{paymentMethodLabel(order.payment_method)}{order.bank_branch ? ` — ${order.bank_branch}` : ""}</dd></div>
              <div><dt>Hali</dt><dd>{order.sandbox ? "Imehakikiwa kwenye sandbox" : order.demo ? "Imekamilika (demo)" : "Malipo yamepokelewa"}</dd></div>
              <div><dt>Jumla ya malipo</dt><dd>TZS {formatMoney(order.amount)}</dd></div>
              {order.coupon_discount > 0 && <div><dt>Punguzo la kuponi ({order.coupon_code})</dt><dd>- TZS {formatMoney(order.coupon_discount)}</dd></div>}
              <div><dt>Kamisheni ya jukwaa ({order.commission_percent}%)</dt><dd>TZS {formatMoney(order.platform_fee)}</dd></div>
              <div><dt>Mgao wa muuzaji baada ya kamisheni</dt><dd>TZS {formatMoney(order.seller_payout)}</dd></div>
              <div><dt>Tarehe</dt><dd>{order.completed_at || "—"}</dd></div>
            </dl>
            {(order.demo || order.sandbox) && <p className="demo-receipt-note">Risiti hii inathibitisha oda ya majaribio pekee; si uthibitisho wa muamala wa pesa halisi.</p>}
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
  const [form, setForm] = useState({ buyer_name: "", buyer_email: "", buyer_phone: "", delivery_address: "", payment_method: "M-Pesa", abandoned_checkout_consent: false });
  const [couponCode, setCouponCode] = useState("");
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

  function updateVariant(productId, variantId) {
    const next = items.map((item) => item.product_id === productId
      ? { ...item, variant_id: variantId }
      : item);
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
    const missingVariant = products.some((product) => (
      product.variants?.length
      && !items.find((item) => item.product_id === product.id)?.variant_id
    ));
    if (missingVariant) {
      setError(translate("Chagua aina ya bidhaa.", language));
      return;
    }
    const unavailableVariant = products.some((product) => {
      if (!product.variants?.length) return false;
      const item = items.find((row) => row.product_id === product.id);
      const variant = product.variants.find((row) => row.id === item?.variant_id);
      return !variant || variant.stock_quantity < (item?.quantity || 0);
    });
    if (unavailableVariant) {
      setError(translate("Stock ya aina hii ya bidhaa haitoshi.", language));
      return;
    }
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
          coupon_code: couponCode,
          abandoned_checkout_consent: form.abandoned_checkout_consent,
          items: items.map((item) => ({
            product_id: item.product_id,
            quantity: item.quantity,
            variant_id: item.variant_id || null,
          })),
        }),
      });
      setCheckoutId(result.checkout_id);
      window.history.replaceState({}, "", `/cart/${result.checkout_id}`);
      if (result.payment_url) window.location.assign(result.payment_url);
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
      sandbox: checkout.sandbox,
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
  const isPesapalSandbox = paymentConfig?.pesapal_sandbox_enabled === true;
  const pesapalSandboxUnavailable =
    paymentConfig?.pesapal_sandbox_requested === true && !isPesapalSandbox;
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
                  <div>
                    <strong>{product.name}</strong><span>Wauzaji: {product.shop_name}</span><small>TZS {formatMoney(product.amount)}</small>
                    {product.variants?.length > 0 && (
                      <select
                        className="text-input"
                        aria-label={`${translate("Aina ya bidhaa", language)} ${product.name}`}
                        value={items.find((item) => item.product_id === product.id)?.variant_id || ""}
                        onChange={(event) => updateVariant(product.id, event.target.value)}
                      >
                        <option value="">Chagua aina ya bidhaa</option>
                        {product.variants.map((variant) => (
                          <option key={variant.id} value={variant.id} disabled={variant.stock_quantity <= 0}>
                            {variant.label} · {translate("Stock inayopatikana", language)}: {variant.stock_quantity}
                          </option>
                        ))}
                      </select>
                    )}
                  </div>
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
                  <label className="checkout-consent">
                    <input
                      type="checkbox"
                      checked={form.abandoned_checkout_consent}
                      onChange={(event) => setForm({ ...form, abandoned_checkout_consent: event.target.checked })}
                    />
                    <span>{translate("Nataka kupokea ukumbusho mmoja wa checkout kupitia email baada ya saa 24 ikiwa sijalipa. Naweza kujiondoa kupitia link kwenye email.", language)}</span>
                  </label>
                  <label className="field-label" htmlFor="cart-coupon">Msimbo wa kuponi (demo pekee)</label>
                  <input id="cart-coupon" className="text-input" maxLength={32} autoComplete="off" value={couponCode} onChange={(event) => setCouponCode(event.target.value.toUpperCase())} />
                  {isAblinerSelected && (
                    <p className="field-hint">Abliner itatuma ombi moja la malipo kwa jumla ya kikapu. Thibitisha kwenye simu yako; usiweke PIN au OTP hapa.</p>
                  )}
                  {isLivePayment && !isAblinerSelected && (
                    <p className="field-hint">ClickPesa itatuma ombi moja la USSD kwa jumla ya kikapu. Ada za ClickPesa au mtandao zinaweza kutumika na zitaonyeshwa kwenye prompt. Thibitisha kwenye simu yako; usiweke PIN au OTP hapa.</p>
                  )}
                  {isPesapalSandbox && <p className="field-hint">Malipo yatafunguliwa kwenye PesaPal Sandbox. Hii ni ya majaribio tu; hakuna pesa halisi itakayokatwa.</p>}
                  {pesapalSandboxUnavailable && <p className="form-error" role="alert">Mipangilio ya PesaPal Sandbox haijakamilika. Wasiliana na msimamizi wa mfumo.</p>}
                  {!isPesapalSandbox && (!isLivePayment || isAblinerLive) && (
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
                  <button
                    className="generate-button"
                    type="submit"
                    disabled={busy || pesapalSandboxUnavailable || products.some((product) => product.variants?.length && !items.find((item) => item.product_id === product.id)?.variant_id)}
                  >
                    {busy ? "Inaandaa bili..." : `Lipa TZS ${formatMoney(total)}`}
                  </button>
                </form>
              )}
              <p className="checkout-footnote">{isAblinerSelected ? "Malipo yatakusanywa kupitia Abliner kwenye akaunti ya jukwaa; mgao wa wauzaji utalipwa kwa mikono." : isLivePayment ? "Malipo yatakusanywa na ClickPesa kwenye akaunti ya jukwaa; mgao wa wauzaji utalipwa kwa mikono." : "Kikapu hiki ni majaribio. Malipo halisi hayajaunganishwa."}</p>
            </>
          )}
          {checkoutId && checkout && (
            <>
              <div className="eyebrow"><span /> {checkout.sandbox ? "PESAPAL SANDBOX" : checkout.demo ? "ODA YA MAJARIBIO" : checkout.payment_method === ABLINER_PAYMENT_METHOD ? "MALIPO YA ABLINER" : "MALIPO YA CLICKPESA"}</div>
              <h1>{checkout.payment_status === "COMPLETED" ? "Malipo yamekamilika" : checkout.demo ? "Gawanya kwa wauzaji" : "Subiri ombi la malipo"}</h1>
              {checkout.payment_status === "COMPLETED" && <img className="receipt-logo" src="/images/liparahisi-logo.svg" alt="LipaRahisi" />}
              {checkout.splits.map((split) => (
                <div className="cart-split" key={split.order_id}>
                  <strong>{split.shop_name}</strong>
                  <span>{split.items.map((item) => `${item.quantity} x ${item.product_name}${item.variant_label ? ` (${item.variant_label})` : ""} @ TZS ${formatMoney(item.unit_amount)} = TZS ${formatMoney(item.quantity * item.unit_amount)}`).join(", ")}</span>
                  <b>TZS {formatMoney(split.amount)}</b>
                  <small>Kamisheni ({checkout.commission_percent}%): TZS {formatMoney(split.platform_fee)}</small>
                  <small>Mgao wa muuzaji baada ya kamisheni: TZS {formatMoney(split.seller_payout)}</small>
                </div>
              ))}
              {checkout.coupon_discount > 0 && <p className="field-hint">{translate("Punguzo la kuponi", language)} ({checkout.coupon_code}): - TZS {formatMoney(checkout.coupon_discount)}</p>}
              <div className="checkout-amount">Jumla ya mteja: TZS {formatMoney(checkout.total_amount)}</div>
              <p className="field-hint">Kamisheni inakatwa kwenye mgao wa kila muuzaji; haiongezwi kwenye bili ya mnunuzi.</p>
              {checkout.payment_status === "PENDING" && checkout.sandbox && <p className="payment-result pending">Malipo ya PesaPal Sandbox yanathibitishwa. Hakuna pesa halisi inayohamishwa.</p>}
              {checkout.payment_status === "PENDING" && checkout.sandbox && checkout.payment_url && <a className="generate-button" href={checkout.payment_url}>Endelea na malipo ya sandbox</a>}
              {checkout.payment_status === "PENDING" && !checkout.demo && !checkout.sandbox && (
                <p className="payment-result pending">
                  {checkout.payment_method === ABLINER_PAYMENT_METHOD
                    ? "Ombi moja la Abliner limetumwa kwa jumla ya kikapu. Thibitisha kwenye simu yako; PIN/OTP haihitajiki kwenye tovuti."
                    : "Ombi moja la malipo limetumwa kwa namba uliyoweka. Ada za ClickPesa au mtandao zinaweza kutumika na zitaonyeshwa kwenye prompt. Thibitisha kwenye simu yako; PIN/OTP haihitajiki kwenye tovuti."}
                </p>
              )}
              {checkout.payment_status === "PENDING" && checkout.demo && (
                <button className="generate-button" type="button" disabled={busy} onClick={completeCart}>{busy ? "Inathibitisha..." : "Thibitisha demo kwa wauzaji wote"}</button>
              )}
              {checkout.payment_status === "COMPLETED" && <p className="success-message">{checkout.sandbox ? "Oda imethibitishwa kwenye PesaPal Sandbox; hakuna pesa halisi iliyohamishwa." : checkout.demo ? "Oda zote za wauzaji zimekamilika kama majaribio." : "Malipo yamepokelewa. Mgao wa wauzaji utalipwa kwa mikono."}</p>}
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
                  {(checkout.demo || checkout.sandbox) && <p className="demo-receipt-note">Risiti hii inathibitisha oda ya majaribio pekee; si uthibitisho wa muamala wa pesa halisi.</p>}
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

function SuspendedAccountDashboard({ user, onLogout }) {
  const localize = useLocalize();
  const [alerts, setAlerts] = useState([]);
  const [drafts, setDrafts] = useState({});
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    api("/security/alerts")
      .then(setAlerts)
      .catch((requestError) => setError(requestError.message));
  }, []);

  async function submitAppeal(alertId) {
    const appealMessage = (drafts[alertId] || "").trim();
    if (appealMessage.length < 10) {
      setError("Andika maelezo ya appeal yenye angalau herufi 10.");
      return;
    }
    try {
      await api(`/security/alerts/${encodeURIComponent(alertId)}/appeal`, {
        method: "POST",
        body: JSON.stringify({ message: appealMessage }),
      });
      setMessage("Appeal imetumwa kwa ukaguzi wa admin.");
      setError("");
      setDrafts({ ...drafts, [alertId]: "" });
      setAlerts(await api("/security/alerts"));
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  return localize(
    <main className="page-shell">
      <Header label="Akaunti ina vikwazo" user={user} onLogout={onLogout} />
      <section className="dashboard-content feature-list">
        <h1>Akaunti imesimamishwa kwa muda</h1>
        <p>Ufikiaji wa huduma umezuiliwa. Unaweza kukagua tahadhari na kuwasilisha appeal kwa timu ya LipaRahisi.</p>
        {message && <p className="form-success" role="status">{message}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        {alerts.map((alert) => (
          <article className="security-detail" key={alert.id}>
            <h2>{alert.category.replaceAll("_", " ")} · {alert.severity}</h2>
            <ul>{alert.reasons.map((reason, index) => <li key={`${index}-${reason}`}>{reason}</li>)}</ul>
            {alert.appeal_status === "PENDING" ? (
              <p className="field-hint">Appeal yako inasubiri ukaguzi.</p>
            ) : (
              <>
                <label className="field-label" htmlFor={`suspended-appeal-${alert.id}`}>Sababu ya kuomba ukaguzi upya</label>
                <textarea
                  id={`suspended-appeal-${alert.id}`}
                  className="text-input security-note"
                  value={drafts[alert.id] || ""}
                  onChange={(event) => setDrafts({ ...drafts, [alert.id]: event.target.value })}
                  maxLength={2000}
                />
                <button type="button" className="table-action" onClick={() => submitAppeal(alert.id)}>Tuma appeal</button>
              </>
            )}
          </article>
        ))}
        {!alerts.length && <p className="empty-state">Hakuna tahadhari iliyopatikana. Wasiliana na huduma kwa wateja kwa msaada.</p>}
      </section>
    </main>,
  );
}

export default function App() {
  const localize = useLocalize();
  const { setLanguage } = useLanguage();
  const checkoutMatch = window.location.pathname.match(/^\/(?:pay|p)\/([a-f0-9]{32})\/?$/i);
  const cartMatch = window.location.pathname.match(/^\/cart(?:\/([A-Z0-9]+))?\/?$/i);
  const storefrontMatch = window.location.pathname.match(/^\/store\/([^/]+)\/?$/);
  const trackingMatch = window.location.pathname.match(/^\/track\/([^/]+)\/?$/);
  const forgotMatch = window.location.pathname === "/forgot-password";
  const resetMatch = window.location.pathname === "/reset-password";
  const verifyMatch = window.location.pathname === "/verify-email";
  const [auth, setAuth] = useState(null);
  const [authState, setAuthState] = useState(null);
  const [authError, setAuthError] = useFeedbackState("error");
  const [showAuth, setShowAuth] = useState(false);

  useEffect(() => {
    const preferredLanguage = auth?.preferred_language || authState?.user?.preferred_language;
    if (preferredLanguage) setLanguage(preferredLanguage);
  }, [auth?.preferred_language, authState?.user?.preferred_language, setLanguage]);

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
  if (trackingMatch) return <PublicTrackingPage trackingNumber={decodeURIComponent(trackingMatch[1])} />;
  if (forgotMatch) return <ForgotPasswordPage />;
  if (resetMatch) return <ResetPasswordPage />;
  if (verifyMatch) return <VerifyEmailPage />;
  if (authError) {
    return localize(<main className="page-shell auth-shell"><Header label="Marketplace" /><p className="form-error">{authError}</p></main>);
  }
  if (!authState) return localize(<main className="page-shell auth-shell"><Header label="Marketplace" /><p className="buyer-intro">Inapakia mfumo...</p></main>);
  if (auth?.status === "SUSPENDED") return <SuspendedAccountDashboard user={auth} onLogout={logout} />;
  if (authState.user?.status === "SUSPENDED") return <SuspendedAccountDashboard user={authState.user} onLogout={logout} />;
  if (auth) {
    if (auth.role === "BUYER") return <BuyerDashboard user={auth} onLogout={logout} />;
    if (auth.role === "DELIVERY_AGENT") return <DeliveryAgentDashboard user={auth} onLogout={logout} />;
    return <MarketplaceDashboard user={auth} onLogout={logout} />;
  }
  if (authState.user) {
    if (authState.user.role === "BUYER") return <BuyerDashboard user={authState.user} onLogout={logout} />;
    if (authState.user.role === "DELIVERY_AGENT") return <DeliveryAgentDashboard user={authState.user} onLogout={logout} />;
    return <MarketplaceDashboard user={authState.user} onLogout={logout} />;
  }
  if (!authState.setup_required && !showAuth) return <PublicMarketplace onLogin={() => setShowAuth(true)} />;
  return <AuthScreen
    setupRequired={authState.setup_required}
    setupAvailable={authState.setup_available}
    onAuth={setAuth}
    onBrowse={() => setShowAuth(false)}
  />;
}
