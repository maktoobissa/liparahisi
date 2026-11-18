import React, { useEffect, useState } from "react";
import QRCode from "qrcode";
import JsBarcode from "jsbarcode";

const API = "/api";

const networks = [
  { value: "M-Pesa", label: "M-Pesa", short: "MP", color: "mpesa" },
  { value: "Tigo Pesa", label: "Tigo Pesa", short: "TP", color: "tigo" },
  { value: "Airtel Money", label: "Airtel Money", short: "AM", color: "airtel" },
  { value: "NMB", label: "NMB", short: "NB", color: "bank" },
  { value: "CRDB", label: "CRDB", short: "CR", color: "bank" },
  { value: "NBC", label: "NBC", short: "NC", color: "bank" },
  { value: "NMB Branch", label: "Tawi la NMB", short: "NT", color: "bank" },
  { value: "CRDB Branch", label: "Tawi la CRDB", short: "CT", color: "bank" },
];

const bankBranches = [
  "Dar es Salaam - Kariakoo",
  "Dar es Salaam - Posta",
  "Dar es Salaam - Ubungo",
  "Mwanza",
  "Arusha",
  "Dodoma",
  "Tawi lingine",
];

function formatMoney(amount) {
  return new Intl.NumberFormat("sw-TZ", { maximumFractionDigits: 0 }).format(amount);
}

function newRequestKey() {
  if (window.crypto?.randomUUID) {
    return window.crypto.randomUUID().replace(/-/g, "");
  }
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

function Header({ label = "Prototype" }) {
  return (
    <header className="topbar">
      <a className="brand" href="/" aria-label="LipaRahisi mwanzo">
        <span className="brand-mark" aria-hidden="true">
          <span /><span /><span /><span />
        </span>
        <span>Lipa<span className="brand-light">Rahisi</span></span>
      </a>
      <span className="prototype-label">
        <span className="status-dot" />
        {label}
      </span>
    </header>
  );
}

function Dashboard() {
  const [product, setProduct] = useState("");
  const [amount, setAmount] = useState("");
  const [merchantPassword, setMerchantPassword] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [merchantPhone, setMerchantPhone] = useState("");
  const [settingsMessage, setSettingsMessage] = useState("");
  const [qrImage, setQrImage] = useState("");
  const [barcodeValue, setBarcodeValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!barcodeValue) return;
    const canvas = document.getElementById("product-barcode");
    if (!canvas) return;
    JsBarcode(canvas, barcodeValue, {
      format: "CODE128",
      lineColor: "#17251f",
      width: 2,
      height: 72,
      displayValue: true,
      font: "DM Sans",
      fontSize: 13,
      margin: 8,
    });
  }, [barcodeValue]);

  async function generate(event) {
    event.preventDefault();
    setError("");
    setQrImage("");
    setBarcodeValue("");

    if (!product.trim()) {
      setError("Jina la bidhaa linahitajika.");
      return;
    }
    if (!Number.isFinite(Number(amount)) || Number(amount) <= 0) {
      setError("Weka bei sahihi ya bidhaa.");
      return;
    }

    setBusy(true);
    try {
      const response = await fetch(`${API}/products`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Merchant-Token": merchantPassword,
        },
        body: JSON.stringify({ name: product.trim(), amount: Number(amount) }),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(responseMessage(result.detail, "Bidhaa haikuweza kuhifadhiwa."));
      }

      const image = await QRCode.toDataURL(result.qr_url, {
        width: 320,
        margin: 2,
        errorCorrectionLevel: "M",
        color: { dark: "#17251f", light: "#ffffff" },
      });
      setQrImage(image);
      setBarcodeValue(result.id);
    } catch (requestError) {
      setError(requestError.message || "QR haikuweza kutengenezwa.");
    } finally {
      setBusy(false);
    }
  }

  function downloadQr() {
    if (!qrImage) return;
    const link = document.createElement("a");
    link.href = qrImage;
    link.download = `${product.trim().replace(/[^\p{L}\p{N}-]+/gu, "-") || "bidhaa"}-qr.png`;
    link.click();
  }

  function downloadBarcode() {
    if (!barcodeValue) return;
    const canvas = document.getElementById("product-barcode");
    if (!canvas) return;
    const link = document.createElement("a");
    link.href = canvas.toDataURL("image/png");
    link.download = `${product.trim().replace(/[^\p{L}\p{N}-]+/gu, "-") || "bidhaa"}-barcode.png`;
    link.click();
  }

  async function updateMerchantSettings(event) {
    event.preventDefault();
    setSettingsMessage("");
    if (newPassword !== confirmPassword) {
      setSettingsMessage("Nenosiri jipya na uthibitisho havifanani.");
      return;
    }
    if (newPassword && newPassword.length < 8) {
      setSettingsMessage("Nenosiri jipya liwe na angalau herufi 8.");
      return;
    }
    if (merchantPhone && !/^(0\d{9}|255\d{9})$/.test(merchantPhone.replace(/\D/g, ""))) {
      setSettingsMessage("Weka namba sahihi ya simu ya Tanzania.");
      return;
    }

    try {
      const response = await fetch(`${API}/merchant/settings`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-Merchant-Token": oldPassword,
        },
        body: JSON.stringify({
          new_password: newPassword || undefined,
          phone: merchantPhone || undefined,
        }),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(responseMessage(result.detail, "Mipangilio haikubadilishwa."));
      }
      setSettingsMessage("Mipangilio ya muuzaji imehifadhiwa.");
      setOldPassword("");
      if (newPassword) setMerchantPassword(newPassword);
      setNewPassword("");
      setConfirmPassword("");
      if (result.phone) setMerchantPhone(result.phone);
    } catch (requestError) {
      setSettingsMessage(requestError.message);
    }
  }

  return (
    <main className="page-shell">
      <Header />
      <section className="intro">
        <div className="eyebrow"><span /> MALIPO YA BIDHAA, KWA URAHISI</div>
        <h1>Tengeneza QR.<br /><span>Lipa kwa urahisi.</span></h1>
        <p>Weka jina na bei ya bidhaa. App itaunda QR na barcode maalum ya bidhaa hiyo.</p>
        <button
          type="button"
          className="settings-toggle"
          onClick={() => setSettingsOpen((open) => !open)}
        >
          {settingsOpen ? "Funga mipangilio" : "Mipangilio ya muuzaji"}
        </button>
      </section>

      {settingsOpen && (
        <form className="merchant-settings" onSubmit={updateMerchantSettings}>
          <div>
            <h2>Mipangilio ya muuzaji</h2>
            <p>Badilisha nenosiri na namba ya kupokea mawasiliano. Usihifadhi PIN kwenye app.</p>
          </div>
          <label className="field-label" htmlFor="old-password">Nenosiri la sasa</label>
          <input
            id="old-password"
            className="text-input"
            type="password"
            autoComplete="current-password"
            value={oldPassword}
            onChange={(event) => setOldPassword(event.target.value)}
            required
          />
          <label className="field-label" htmlFor="new-password">Nenosiri jipya</label>
          <input
            id="new-password"
            className="text-input"
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            placeholder="Angalau herufi 8"
          />
          <label className="field-label" htmlFor="confirm-password">Thibitisha nenosiri jipya</label>
          <input
            id="confirm-password"
            className="text-input"
            type="password"
            autoComplete="new-password"
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
          />
          <label className="field-label" htmlFor="merchant-phone">Namba ya muuzaji</label>
          <input
            id="merchant-phone"
            className="text-input"
            type="tel"
            inputMode="tel"
            value={merchantPhone}
            onChange={(event) => setMerchantPhone(event.target.value)}
            placeholder="Mf. 0712 345 678"
          />
          {settingsMessage && <p className="form-error" role="alert">{settingsMessage}</p>}
          <button className="generate-button" type="submit">Hifadhi mipangilio</button>
        </form>
      )}

      <section className="workspace" aria-label="Tengeneza QR na barcode ya bidhaa">
        <form className="form-card" onSubmit={generate} noValidate>
          <div className="section-heading">
            <span className="step-number">01</span>
            <div>
              <h2>Maelezo ya bidhaa</h2>
              <p>Jaza bidhaa unayotaka kuuza.</p>
            </div>
          </div>

          <label className="field-label" htmlFor="merchant-password">Nenosiri la muuzaji</label>
          <input
            id="merchant-password"
            className="text-input"
            type="password"
            autoComplete="current-password"
            placeholder="Weka nenosiri"
            value={merchantPassword}
            onChange={(event) => setMerchantPassword(event.target.value)}
            required
          />

          <label className="field-label" htmlFor="product">Jina la bidhaa</label>
          <input
            id="product"
            className="text-input"
            type="text"
            placeholder="Mf. Kikombe cha kahawa"
            value={product}
            onChange={(event) => setProduct(event.target.value)}
            maxLength={80}
            required
          />

          <label className="field-label" htmlFor="amount">Bei ya bidhaa</label>
          <div className="amount-input">
            <span>TZS</span>
            <input
              id="amount"
              type="number"
              min="1"
              step="1"
              placeholder="0"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              required
            />
            <span className="currency-name">Shilingi</span>
          </div>

          {error && <p className="form-error" role="alert">{error}</p>}

          <button className="generate-button" type="submit" disabled={busy}>
            {busy ? "Inatengeneza..." : "Tengeneza QR na barcode"}
            {!busy && <span aria-hidden="true">↗</span>}
          </button>

          <div className="secure-note">
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M4.5 6V4.5a3.5 3.5 0 0 1 7 0V6M3 6h10v8H3z" />
              <path d="M8 9v2" />
            </svg>
            Nenosiri linahitajika kutengeneza bidhaa.
          </div>
        </form>

        <aside className="result-card" aria-live="polite">
          <div className="result-topline">
            <div className="section-heading result-heading">
              <span className="step-number">02</span>
              <div>
                <h2>QR na barcode ya bidhaa</h2>
                <p>Zote zinatambulisha bidhaa hii tu.</p>
              </div>
            </div>
            <span className="qr-tag">QR</span>
          </div>

          <div className={`qr-frame ${qrImage ? "has-qr" : ""}`}>
            {qrImage ? (
              <img src={qrImage} alt={`QR ya ${product}`} />
            ) : (
              <div className="qr-placeholder" aria-hidden="true">
                <span className="corner top-left" />
                <span className="corner top-right" />
                <span className="corner bottom-left" />
                <span className="placeholder-center">
                  <span className="mini-qr"><i /><i /><i /><i /><i /><i /><i /><i /><i /></span>
                </span>
              </div>
            )}
          </div>

          {barcodeValue && (
            <div className="barcode-preview">
              <canvas id="product-barcode" aria-label={`Barcode ya ${barcodeValue}`} />
              <button type="button" className="barcode-download" onClick={downloadBarcode}>
                Pakua barcode
              </button>
            </div>
          )}

          <div className="price-preview">
            <span>{product.trim() || "Jina la bidhaa"}</span>
            <strong>TZS {amount && Number(amount) > 0 ? formatMoney(Number(amount)) : "0"}</strong>
          </div>

          <div className="result-actions">
            <button type="button" onClick={downloadQr} disabled={!qrImage}>
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M10 3v9m0 0 3.5-3.5M10 12 6.5 8.5M4 13v3h12v-3" />
              </svg>
              Pakua QR
            </button>
            <button type="button" className="copy-button" disabled={!qrImage}>
              Inahifadhiwa
            </button>
          </div>

          <div className="demo-notice">
            <span className="notice-icon" aria-hidden="true">i</span>
            <p><strong>QR ya bidhaa.</strong> Baada ya kuscan, mtumiaji atachagua mtandao wa malipo na kurisa risiti.</p>
          </div>
        </aside>
      </section>
      <footer>
        <span>© 2026 LipaRahisi</span>
        <span>QR na barcode ni maalum kwa kila bidhaa</span>
      </footer>
    </main>
  );
}

function CheckoutPage({ productId }) {
  const [product, setProduct] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    window.location.replace("/");
  }, []);

  if (!product) {
    return (
      <main className="page-shell checkout-shell">
        <Header label="Malipo ya bidhaa" />
        {error ? <p className="form-error" role="alert">{error}</p> : <p className="buyer-intro">Inapakia bidhaa...</p>}
      </main>
    );
  }

  return (
    <main className="page-shell checkout-shell">
      <Header label="Malipo ya bidhaa" />
      <section className="intro">
        <div className="eyebrow"><span /> BIDHAA IMETAMBULIKA</div>
        <h1>{product.name}<br /><span>TZS {formatMoney(product.amount)}</span></h1>
        <p>QR imeendelea kwenye fomu ya awali ya app. Malipo na risiti zitaunganishwa baadaye.</p>
      </section>
      <footer>
        <span>© 2026 LipaRahisi</span>
        <span>Malipo ya bidhaa kwa risiti</span>
      </footer>
    </main>
  );
}

export default function App() {
  const match = window.location.pathname.match(/^\/(?:pay|p)\/([a-f0-9]{32})\/?$/i);
  return match ? <CheckoutPage productId={match[1]} /> : <Dashboard />;
}
