import React, { useEffect, useRef, useState } from "react";
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
  return new Intl.NumberFormat("sw-TZ", { maximumFractionDigits: 0 }).format(Number(amount) || 0);
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
  const response = await fetch(`${API}${path}`, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(responseMessage(result.detail, "Ombi halikukamilika."));
  return result;
}

function Header({ label = "Demo", user, onLogout }) {
  return (
    <header className="topbar">
      <a className="brand" href="/" aria-label="LipaRahisi mwanzo">
        <span className="brand-mark" aria-hidden="true"><span /><span /><span /><span /></span>
        <span>Lipa<span className="brand-light">Rahisi</span></span>
      </a>
      <div className="header-right">
        <span className="prototype-label"><span className="status-dot" />{label}</span>
        {user && (
          <button className="logout-button" type="button" onClick={onLogout}>
            Toka ({user.full_name})
          </button>
        )}
      </div>
    </header>
  );
}

function AuthScreen({ setupRequired, setupAvailable, onAuth }) {
  const [mode, setMode] = useState(setupRequired ? "setup" : "login");
  const [form, setForm] = useState({
    setup_token: "",
    full_name: "",
    email: "",
    password: "",
    shop_name: "",
    phone: "",
  });
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
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
      } else if (mode === "register") {
        const result = await api("/auth/register", {
          method: "POST",
          body: JSON.stringify({
            full_name: form.full_name,
            email: form.email,
            password: form.password,
            shop_name: form.shop_name,
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
  const isRegister = mode === "register";
  return (
    <main className="page-shell auth-shell">
      <Header label="Marketplace" />
      <section className="auth-card">
        <div className="eyebrow"><span /> {isSetup ? "USIMAMIZI MKUU" : isRegister ? "JIUNGE KAMA MUUZAJI" : "KARIBU TENA"}</div>
        <h1>{isSetup ? "Fungua akaunti ya admin" : isRegister ? "Sajili duka lako" : "Ingia LipaRahisi"}</h1>
        <p className="buyer-intro">
          {isSetup
            ? "Akaunti hii itakuwa msimamizi mkuu wa marketplace na itaidhinisha wauzaji."
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
              <label className="field-label" htmlFor="shop-name">Jina la duka</label>
              <input id="shop-name" className="text-input" name="shop_name" required minLength={2} value={form.shop_name} onChange={change} />
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
        {!setupRequired && (
          <div className="auth-switch">
            {isRegister ? "Tayari una akaunti?" : "Unataka kuuza bidhaa?"}
            <button type="button" onClick={() => { setError(""); setMessage(""); setMode(isRegister ? "login" : "register"); }}>
              {isRegister ? "Ingia" : "Jisajili hapa"}
            </button>
          </div>
        )}
      </section>
      <footer><span>© 2026 LipaRahisi</span><span>PIN au OTP haiombwi hapa</span></footer>
    </main>
  );
}

function ProductTile({ product, onArchive, canArchive }) {
  const [qr, setQr] = useState("");
  const barcodeRef = useRef(null);
  useEffect(() => {
    let active = true;
    QRCode.toDataURL(product.qr_url, {
      width: 320,
      margin: 1,
      errorCorrectionLevel: "M",
      color: { dark: "#17251f", light: "#ffffff" },
    }).then((image) => {
      if (active) setQr(image);
    }).catch(() => {});
    if (barcodeRef.current) {
      JsBarcode(barcodeRef.current, product.id, {
        format: "CODE128",
        lineColor: "#17251f",
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

  return (
    <article className={`product-tile ${product.is_active ? "" : "product-inactive"}`}>
      {qr ? <img className="product-qr" src={qr} alt={`QR ya ${product.name}`} /> : <div className="product-qr qr-placeholder" />}
      <div className="product-tile-details">
        <strong>{product.name}</strong>
        <span>TZS {formatMoney(product.amount)}</span>
        <small>Stock: {product.stock_quantity ?? 0}</small>
        {product.shop_name && <small>{product.shop_name}</small>}
        {!product.is_active && <small className="muted-status">Imeondolewa</small>}
        <div className="product-tile-actions">
          <button type="button" onClick={downloadQr} disabled={!qr}>Pakua QR</button>
          <button type="button" onClick={downloadBarcode}>Pakua barcode</button>
          {canArchive && product.is_active && <button type="button" onClick={() => onArchive(product.id)}>Ondoa</button>}
        </div>
      </div>
      <svg ref={barcodeRef} className="product-barcode" aria-label={`Barcode ya ${product.name}`} />
    </article>
  );
}

function StatCard({ title, value, note }) {
  return (
    <article className="stat-card">
      <span>{title}</span>
      <strong>{value}</strong>
      {note && <small>{note}</small>}
    </article>
  );
}

function MarketplaceDashboard({ user, onLogout }) {
  const [tab, setTab] = useState("overview");
  const [stats, setStats] = useState(null);
  const [products, setProducts] = useState([]);
  const [sales, setSales] = useState([]);
  const [sellers, setSellers] = useState([]);
  const [auditRows, setAuditRows] = useState([]);
  const [productForm, setProductForm] = useState({ name: "", amount: "", cost_amount: "", stock_quantity: "" });
  const [productQuery, setProductQuery] = useState("");
  const [passwordForm, setPasswordForm] = useState({ current_password: "", new_password: "" });
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const isAdmin = user.role === "ADMIN";

  async function refresh() {
    const [dashboard, productRows, salesRows] = await Promise.all([
      api("/dashboard"),
      api(`/products?q=${encodeURIComponent(productQuery)}`),
      api("/sales"),
    ]);
    setStats(dashboard);
    setProducts(productRows);
    setSales(salesRows);
    if (isAdmin) setSellers(await api("/admin/sellers"));
    if (isAdmin) setAuditRows(await api("/admin/audit"));
  }

  useEffect(() => {
    refresh().catch((requestError) => setError(requestError.message));
  }, []);

  async function createProduct(event) {
    event.preventDefault();
    setMessage("");
    setError("");
    setBusy(true);
    try {
      await api("/products", {
        method: "POST",
        body: JSON.stringify({
          name: productForm.name.trim(),
          amount: Number(productForm.amount),
          cost_amount: Number(productForm.cost_amount) || 0,
          stock_quantity: Number(productForm.stock_quantity) || 0,
        }),
      });
      setProductForm({ name: "", amount: "", cost_amount: "", stock_quantity: "" });
      setMessage("Bidhaa imehifadhiwa. QR na barcode yake vipo kwenye orodha hapa chini.");
      await refresh();
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
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

  const nav = [
    ["overview", "Muhtasari"],
    ["products", "Bidhaa"],
    ["sales", "Mauzo"],
    ...(isAdmin ? [["sellers", "Wauzaji"]] : []),
    ...(isAdmin ? [["audit", "Audit"]] : []),
    ["settings", "Akaunti"],
  ];

  return (
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
          <SalesTable sales={sales.slice(0, 5)} isAdmin={isAdmin} onArchive={archiveSale} />
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
                <label className="field-label" htmlFor="product-amount">Bei ya kuuza (TZS)</label>
                <input id="product-amount" className="text-input" type="number" min="1" step="1" required value={productForm.amount} onChange={(event) => setProductForm({ ...productForm, amount: event.target.value })} />
                <label className="field-label" htmlFor="product-cost">Gharama ya bidhaa (TZS)</label>
                <input id="product-cost" className="text-input" type="number" min="0" step="1" value={productForm.cost_amount} onChange={(event) => setProductForm({ ...productForm, cost_amount: event.target.value })} />
                <label className="field-label" htmlFor="product-stock">Stock inayopatikana</label>
                <input id="product-stock" className="text-input" type="number" min="0" step="1" required value={productForm.stock_quantity} onChange={(event) => setProductForm({ ...productForm, stock_quantity: event.target.value })} />
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
          <div className="dashboard-section-heading"><h2>{isAdmin ? "Bidhaa zote" : "Bidhaa zako"}</h2><span>{products.filter((item) => item.is_active).length} hai</span></div>
          <div className="product-search">
            <input className="text-input" type="search" placeholder="Tafuta bidhaa au duka..." value={productQuery} onChange={(event) => setProductQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") refresh(); }} />
            <button type="button" className="table-action" onClick={() => refresh()}>Tafuta</button>
          </div>
          {products.length ? (
            <div className="product-grid">
              {products.map((product) => <ProductTile key={product.id} product={product} canArchive={!isAdmin} onArchive={archiveProduct} />)}
            </div>
          ) : <p className="empty-state">{isAdmin ? "Hakuna bidhaa bado." : "Bado hujaongeza bidhaa."}</p>}
        </section>
      )}

      {tab === "sales" && (
        <section className="dashboard-content">
          <div className="dashboard-section-heading"><h2>{isAdmin ? "Rekodi zote za mauzo" : "Historia ya mauzo yako"}</h2><span>{sales.length} rekodi</span></div>
          <SalesTable sales={sales} isAdmin={isAdmin} onArchive={archiveSale} />
          {!isAdmin && <p className="field-hint">Ukificha rekodi, itaondoka kwenye orodha yako lakini admin ataendelea kuiona.</p>}
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
            <a className="table-action" href="/api/admin/export/sales">Pakua mauzo CSV</a>
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

      <footer><span>© 2026 LipaRahisi</span><span>Data ya akaunti na mauzo hutenganishwa kwa muuzaji</span></footer>
    </main>
  );
}

function SalesTable({ sales, isAdmin, onArchive }) {
  if (!sales.length) return <p className="empty-state">Hakuna mauzo yanayoonekana bado.</p>;
  return (
    <div className="table-scroll">
      <table className="sales-table">
        <thead><tr><th>Oda / Bidhaa</th>{isAdmin && <th>Muuzaji</th>}<th>Mnunuzi</th><th>Hali</th><th>Kiasi</th><th>Commission</th>{!isAdmin && <th />}</tr></thead>
        <tbody>
          {sales.map((sale) => (
            <tr key={sale.order_id} className={sale.archived_at ? "archived-row" : ""}>
              <td><strong>{sale.product_name}</strong><small>{sale.order_id}</small></td>
              {isAdmin && <td>{sale.shop_name || "—"}</td>}
              <td>{sale.buyer_name || "—"}<small>{sale.payment_method || ""}</small></td>
              <td><span className={`order-status ${sale.payment_status.toLowerCase()}`}>{sale.payment_status === "COMPLETED" ? "Imekamilika (demo)" : sale.payment_status}</span>{sale.archived_at && <small>Imefichwa na muuzaji</small>}</td>
              <td>TZS {formatMoney(sale.amount)}</td>
              <td>TZS {formatMoney(sale.platform_fee)}</td>
              {!isAdmin && <td>{sale.payment_status === "COMPLETED" && !sale.archived_at && <button className="table-action" type="button" onClick={() => onArchive(sale.order_id)}>Ficha</button>}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
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
  const [product, setProduct] = useState(null);
  const [form, setForm] = useState({ buyer_name: "", buyer_email: "", buyer_phone: "", payment_method: "M-Pesa", bank_branch: "", bank_branch_name: "" });
  const [orderId, setOrderId] = useState(new URLSearchParams(window.location.search).get("order_id") || "");
  const [order, setOrder] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [cartCount, setCartCount] = useState(() => loadCart().reduce((sum, item) => sum + item.quantity, 0));

  useEffect(() => {
    api(`/products/${productId}`).then(setProduct).catch((requestError) => setError(requestError.message));
  }, [productId]);

  useEffect(() => {
    if (!orderId) return;
    api(`/orders/${encodeURIComponent(orderId)}`).then(setOrder).catch((requestError) => setError(requestError.message));
  }, [orderId]);

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
      const bankBranch = form.bank_branch === "Tawi lingine" ? form.bank_branch_name.trim() : form.bank_branch;
      if (form.payment_method.endsWith("Branch") && !bankBranch) {
        throw new Error("Weka jina la tawi la benki.");
      }
      const result = await api(`/products/${productId}/checkout`, {
        method: "POST",
        body: JSON.stringify({
          buyer_name: form.buyer_name,
          buyer_email: form.buyer_email,
          buyer_phone: form.buyer_phone,
          payment_method: form.payment_method,
          bank_branch: bankBranch,
          request_key: newRequestKey(),
        }),
      });
      if (!result.order_id) throw new Error("Mfumo haukurudisha namba ya oda.");
      setOrderId(result.order_id);
      window.history.replaceState({}, "", `/pay/${productId}?order_id=${encodeURIComponent(result.order_id)}&demo=1`);
      const status = await api(`/orders/${encodeURIComponent(result.order_id)}`);
      setOrder(status);
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
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  function downloadDocument() {
    if (!order || order.payment_status !== "COMPLETED") return;
    const html = `<!doctype html><html><meta charset="utf-8"><body><h1>LipaRahisi — Risiti ya majaribio</h1><p>Oda: ${escapeHtml(order.order_id)}</p><p>Bidhaa: ${escapeHtml(order.product_name)}</p><p>Mteja: ${escapeHtml(order.buyer_name)}</p><p>Njia: ${escapeHtml(order.payment_method)}</p><p>Kiasi: TZS ${formatMoney(order.amount)}</p><p>Hali: Imekamilika (demo; hakuna pesa halisi)</p><p>Tarehe: ${escapeHtml(order.completed_at || "")}</p></body></html>`;
    const link = document.createElement("a");
    const objectUrl = URL.createObjectURL(new Blob([html], { type: "application/msword;charset=utf-8" }));
    link.href = objectUrl;
    link.download = `risiti-${order.order_id}.doc`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }

  const isCompleted = order?.payment_status === "COMPLETED";
  const selectedMethod = networks.find((method) => method.value === form.payment_method);
  return (
    <main className="page-shell checkout-shell">
      <Header label="Malipo ya bidhaa" />
      <div className="checkout-layout">
        {!product && !error ? <p className="buyer-intro">Inapakia bidhaa...</p> : null}
        {error && (!orderId || !order) && <div className="checkout-card"><p className="form-error" role="alert">{error}</p><a className="retry-link" href="/">Rudi mwanzo</a></div>}
        {product && !orderId && (
          <section className="checkout-card">
            <div className="eyebrow"><span /> BIDHAA IMETAMBULIKA</div>
            <h1>{product.name}</h1>
            <div className="checkout-amount">TZS {formatMoney(product.amount)}</div>
            <p className="buyer-intro">Jaza taarifa za oda na uchague njia ya malipo. PIN au OTP haiingizwe kwenye ukurasa huu.</p>
            <form className="buyer-form" onSubmit={submitCheckout}>
              <label className="field-label" htmlFor="buyer-name">Jina la mnunuzi</label>
              <input id="buyer-name" className="text-input" autoComplete="name" required minLength={2} value={form.buyer_name} onChange={(event) => setForm({ ...form, buyer_name: event.target.value })} />
              <label className="field-label" htmlFor="buyer-email">Barua pepe ya risiti</label>
              <input id="buyer-email" className="text-input" type="email" autoComplete="email" required value={form.buyer_email} onChange={(event) => setForm({ ...form, buyer_email: event.target.value })} />
              <label className="field-label" htmlFor="buyer-phone">Namba ya simu</label>
              <input id="buyer-phone" className="text-input" type="tel" inputMode="tel" autoComplete="tel" placeholder="Mf. 0712 345 678" required value={form.buyer_phone} onChange={(event) => setForm({ ...form, buyer_phone: event.target.value })} />
              <fieldset className="payment-fieldset">
                <legend>Chagua mtandao wa malipo</legend>
                <div className="method-grid">
                  {networks.map((method) => (
                    <button key={method.value} type="button" className={`method-option ${form.payment_method === method.value ? "selected" : ""}`} onClick={() => setForm({ ...form, payment_method: method.value, bank_branch: method.value.endsWith("Branch") ? form.bank_branch : "", bank_branch_name: method.value.endsWith("Branch") ? form.bank_branch_name : "" })}>
                      <span className={`method-icon ${method.color}`}>{method.short}</span>{method.label}<span className="radio-indicator" />
                    </button>
                  ))}
                </div>
              </fieldset>
              {form.payment_method.endsWith("Branch") && (
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
              <button className="generate-button" type="submit" disabled={busy}>{busy ? "Inaandaa oda..." : `Endelea — TZS ${formatMoney(product.amount)}`}</button>
            </form>
            <button className="cart-secondary-button" type="button" onClick={addToCart}>Ongeza bidhaa nyingine kwenye kikapu</button>
            {cartCount > 0 && <a className="cart-link" href="/cart">Nenda kikapu ({cartCount})</a>}
            <p className="checkout-footnote">Huu ni mfumo wa majaribio: hakuna pesa inayokatwa au kuhamishwa kwa muuzaji.</p>
          </section>
        )}
        {orderId && order && !isCompleted && (
          <section className="checkout-card">
            <div className="eyebrow"><span /> MALIPO YA MAJARIBIO</div>
            <h1>Thibitisha oda yako</h1>
            <div className="receipt-product"><span>{order.product_name || product?.name}</span><strong>TZS {formatMoney(order.amount)}</strong></div>
            <div className="payment-result pending"><strong>Haijalipwa bado</strong><span>Njia uliyochagua: {order.payment_method || selectedMethod?.label}{order.bank_branch ? ` — ${order.bank_branch}` : ""}.</span><span>Bonyeza kitufe chini kuthibitisha demo. Hakuna pesa halisi itakayohamishwa.</span></div>
            {error && <p className="form-error" role="alert">{error}</p>}
            <button className="generate-button" type="button" disabled={busy} onClick={completeDemo}>{busy ? "Inathibitisha..." : "Thibitisha malipo ya majaribio"}</button>
          </section>
        )}
        {orderId && isCompleted && (
          <section className="checkout-card receipt-card">
            <div className="receipt-success" aria-hidden="true">✓</div>
            <p className="receipt-kicker">RISITI YA MAJARIBIO</p>
            <h1>Oda imekamilika</h1>
            <div className="receipt-product"><span>{order.product_name || product?.name}</span><strong>TZS {formatMoney(order.amount)}</strong></div>
            <dl className="receipt-details">
              <div><dt>Namba ya oda</dt><dd>{order.order_id}</dd></div>
              <div><dt>Mnunuzi</dt><dd>{order.buyer_name}</dd></div>
              <div><dt>Njia ya malipo</dt><dd>{order.payment_method}{order.bank_branch ? ` — ${order.bank_branch}` : ""}</dd></div>
              <div><dt>Hali</dt><dd>Imekamilika (demo)</dd></div>
              <div><dt>Kamisheni ya jukwaa</dt><dd>TZS {formatMoney(order.platform_fee)}</dd></div>
              <div><dt>Tarehe</dt><dd>{order.completed_at || "—"}</dd></div>
            </dl>
            <p className="demo-receipt-note">Risiti hii inathibitisha oda ya majaribio pekee; si uthibitisho wa muamala wa pesa halisi.</p>
            <button type="button" className="document-button" onClick={() => window.print()}>Chapisha / hifadhi kama PDF</button>
            <button type="button" className="document-button" onClick={downloadDocument}>Pakua risiti ya Word (.doc)</button>
            <a className="retry-link" href="/">Rudi mwanzo</a>
          </section>
        )}
      </div>
      <footer><span>© 2026 LipaRahisi</span><span>{product?.shop_name || "Malipo ya bidhaa"}</span></footer>
    </main>
  );
}

function CartPage({ checkoutId: initialCheckoutId = "" }) {
  const [items, setItems] = useState(loadCart());
  const [products, setProducts] = useState([]);
  const [checkoutId, setCheckoutId] = useState(initialCheckoutId);
  const [checkout, setCheckout] = useState(null);
  const [form, setForm] = useState({ buyer_name: "", buyer_email: "", buyer_phone: "", payment_method: "M-Pesa", bank_branch: "", bank_branch_name: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all(items.map((item) => api(`/products/${item.product_id}`).then((product) => ({ ...item, ...product })).catch(() => null)))
      .then((rows) => setProducts(rows.filter(Boolean)));
  }, []);

  useEffect(() => {
    if (!checkoutId) return;
    api(`/checkouts/${encodeURIComponent(checkoutId)}`).then(setCheckout).catch((requestError) => setError(requestError.message));
  }, [checkoutId]);

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
          payment_method: form.payment_method,
          bank_branch: form.bank_branch,
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
      saveCart([]);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  function downloadCartReceipt() {
    if (!checkout || checkout.payment_status !== "COMPLETED") return;
    const itemsHtml = checkout.splits.map((split) => `
      <h3>${escapeHtml(split.shop_name)}</h3>
      ${split.items.map((item) => `<p>${item.quantity} x ${escapeHtml(item.product_name)} — TZS ${formatMoney(item.quantity * item.unit_amount)}</p>`).join("")}
      <p><strong>Jumla ya muuzaji: TZS ${formatMoney(split.amount)}</strong></p>
      <p>Kamisheni ya jukwaa (5%): TZS ${formatMoney(split.platform_fee)}</p>
    `).join("");
    const html = `<!doctype html><html><meta charset="utf-8"><body>
      <h1>LipaRahisi — Risiti ya kikapu (majaribio)</h1>
      <p>Oda: ${escapeHtml(checkout.checkout_id)}</p>
      <p>Mnunuzi: ${escapeHtml(checkout.buyer_name)}</p>
      <p>Barua pepe: ${escapeHtml(checkout.buyer_email)}</p>
      <p>Simu: ${escapeHtml(checkout.buyer_phone)}</p>
      <p>Njia: ${escapeHtml(checkout.payment_method)}${checkout.bank_branch ? ` — ${escapeHtml(checkout.bank_branch)}` : ""}</p>
      ${itemsHtml}
      <h2>Jumla ya bili: TZS ${formatMoney(checkout.total_amount)}</h2>
      <p>Hali: Imekamilika (demo; hakuna pesa halisi)</p>
      <p>Tarehe: ${escapeHtml(checkout.completed_at || "")}</p>
    </body></html>`;
    const link = document.createElement("a");
    const objectUrl = URL.createObjectURL(new Blob([html], { type: "application/msword;charset=utf-8" }));
    link.href = objectUrl;
    link.download = `risiti-kikapu-${checkout.checkout_id}.doc`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }

  const total = products.reduce((sum, product) => sum + product.amount * (items.find((item) => item.product_id === product.id)?.quantity || 0), 0);

  return (
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
                  <fieldset className="payment-fieldset">
                    <legend>Chagua mtandao wa malipo</legend>
                    <div className="method-grid">
                      {networks.map((method) => (
                        <button key={method.value} type="button" className={`method-option ${form.payment_method === method.value ? "selected" : ""}`} onClick={() => setForm({ ...form, payment_method: method.value })}>
                          <span className={`method-icon ${method.color}`}>{method.short}</span>{method.label}<span className="radio-indicator" />
                        </button>
                      ))}
                    </div>
                  </fieldset>
                  {error && <p className="form-error" role="alert">{error}</p>}
                  <button className="generate-button" type="submit" disabled={busy}>{busy ? "Inaandaa bili..." : `Lipa TZS ${formatMoney(total)}`}</button>
                </form>
              )}
              <p className="checkout-footnote">Kikapu hiki ni majaribio. Malipo halisi hayajaunganishwa.</p>
            </>
          )}
          {checkoutId && checkout && (
            <>
              <div className="eyebrow"><span /> ODA YA MAJARIBIO</div>
              <h1>{checkout.payment_status === "COMPLETED" ? "Malipo yamekamilika" : "Gawanya kwa wauzaji"}</h1>
              {checkout.splits.map((split) => (
                <div className="cart-split" key={split.order_id}>
                  <strong>{split.shop_name}</strong>
                  <span>{split.items.map((item) => `${item.quantity} x ${item.product_name}`).join(", ")}</span>
                  <b>TZS {formatMoney(split.amount)}</b>
                  <small>Kamisheni ya jukwaa: TZS {formatMoney(split.platform_fee)}</small>
                </div>
              ))}
              <div className="checkout-amount">Jumla: TZS {formatMoney(checkout.total_amount)}</div>
              {checkout.payment_status === "PENDING" && (
                <button className="generate-button" type="button" disabled={busy} onClick={completeCart}>{busy ? "Inathibitisha..." : "Thibitisha demo kwa wauzaji wote"}</button>
              )}
              {checkout.payment_status === "COMPLETED" && <p className="success-message">Oda zote za wauzaji zimekamilika kama majaribio.</p>}
              {checkout.payment_status === "EXPIRED" && <p className="form-error">Oda imekwisha muda; anzisha kikapu kipya.</p>}
              {error && <p className="form-error" role="alert">{error}</p>}
              {checkout.payment_status === "COMPLETED" && (
                <>
                  <dl className="receipt-details">
                    <div><dt>Mnunuzi</dt><dd>{checkout.buyer_name}</dd></div>
                    <div><dt>Njia ya malipo</dt><dd>{checkout.payment_method}{checkout.bank_branch ? ` — ${checkout.bank_branch}` : ""}</dd></div>
                    <div><dt>Kamisheni ya jukwaa</dt><dd>TZS {formatMoney(checkout.platform_fee)}</dd></div>
                    <div><dt>Tarehe</dt><dd>{checkout.completed_at || "—"}</dd></div>
                  </dl>
                  <button type="button" className="document-button" onClick={() => window.print()}>Chapisha / hifadhi risiti kama PDF</button>
                  <button type="button" className="document-button" onClick={downloadCartReceipt}>Pakua risiti ya kikapu (.doc)</button>
                </>
              )}
              <a className="retry-link" href="/">Rudi mwanzo</a>
            </>
          )}
        </section>
      </div>
      <footer><span>© 2026 LipaRahisi</span><span>QR ya bidhaa ni reusable; oda ya malipo ina muda wa saa 24</span></footer>
    </main>
  );
}

export default function App() {
  const checkoutMatch = window.location.pathname.match(/^\/(?:pay|p)\/([a-f0-9]{32})\/?$/i);
  const cartMatch = window.location.pathname.match(/^\/cart(?:\/([A-Z0-9]+))?\/?$/i);
  const [auth, setAuth] = useState(null);
  const [authState, setAuthState] = useState(null);
  const [authError, setAuthError] = useState("");

  useEffect(() => {
    if (checkoutMatch || cartMatch) return;
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
  if (authError) {
    return <main className="page-shell auth-shell"><Header label="Marketplace" /><p className="form-error">{authError}</p></main>;
  }
  if (!authState) return <main className="page-shell auth-shell"><Header label="Marketplace" /><p className="buyer-intro">Inapakia mfumo...</p></main>;
  if (auth) return <MarketplaceDashboard user={auth} onLogout={logout} />;
  if (authState.user) return <MarketplaceDashboard user={authState.user} onLogout={logout} />;
  return <AuthScreen setupRequired={authState.setup_required} setupAvailable={authState.setup_available} onAuth={setAuth} />;
}
