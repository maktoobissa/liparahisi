# LipaRahisi

LipaRahisi is a Swahili-first multi-seller marketplace prototype. Sellers can
register, wait for the main administrator's approval, manage their storefront,
products, stock, and sales. Buyers can browse, register, save products, follow
orders, submit support requests, or check out by QR and receive a demo receipt.
The main administrator manages sellers, categories, moderation, and marketplace
sales.

## Marketplace features

- Public catalog with product descriptions, optional JPEG/PNG/WebP product
  image uploads (up to 5 MiB), external image URLs, categories, search, and
  seller storefront pages.
- Buyer accounts with wishlists and account-linked order history. Guest orders
  remain available by their order link but are not attached to an account.
- Seller storefront profiles, stock thresholds and movement history, product
  CSV import/export, and date/status-filtered sales.
- Order delivery status and tracking codes, buyer cancellation requests, and
  verified-order reviews subject to administrator moderation.
- Buyer support tickets with administrator responses.
- Downloadable receipt PDFs with item subtotals, total charged, commission per
  seller, and seller payouts.
  - Swahili, English, and French interface language selection, plus in-app
    pop-up notifications with optional sound and browser notifications.
  - Installable PWA with an offline-cached application shell. Marketplace data
    and checkout still require the API to be online.

Uploaded product images are stored with marketplace data and served through the
public catalog image endpoint; storefront images still use HTTP/HTTPS URLs.
Seller sales/dashboard data refreshes automatically while the page is visible,
and pending order/checkout status is rechecked periodically. Browser
notifications require the visitor's permission; sound can be muted in the app.
Push delivery while the app is closed is not configured. Approved
cancellation of a completed order restores stock only in demo mode; it does not
issue a real payment refund. M-Pesa and Halo Pesa can be selected in demo
checkout, but neither is connected to a live payment provider. Coupons are not
included in this feature set.

## Important payment limitation

The application currently runs in demo-payment mode. Completing checkout
records a clearly marked demo transaction; no mobile-money or bank payment is
made, and no money is transferred to a seller. The 5% commission and seller
proceeds are recorded in the ledger for testing. Provider fees are zero in demo
mode. Do not describe demo orders as real paid transactions.

Live payments require provider approval, seller-specific merchant accounts,
settlement/split-payment support, verified payment callbacks, and production
testing. The app deliberately refuses to start live checkout until that work
has been completed. Do not collect a buyer's PIN or OTP in this app.
The current Selcom helper is not connected to checkout or payment callbacks;
setting Selcom credentials or `SELCOM_LIVE_ENABLED` does not enable real
payments. The integration status therefore keeps `live_payments_enabled` false.

## Local setup

Requirements: Python 3.10+, Node.js, and npm.

In PowerShell, from the project directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
npm install
```

Set `ADMIN_SETUP_TOKEN` in `.env` to a long random secret. Start the API:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

In another PowerShell window:

```powershell
npm run dev -- --host 0.0.0.0
```

Open the Vite URL and create the main administrator using the setup token.
After that, sellers can register and the administrator can approve their
accounts. By default, local records are stored in `backend/data.sqlite3`.
Setting `DATABASE_URL` selects PostgreSQL instead.

## Local Compose stack

Docker Desktop with Docker Compose v2 is required. The public API gateway
forwards authentication, order/payment, catalog, and admin routes to separate
internal APIs; email delivery is also a separate service. Identity, Orders,
Admin, Catalog, and Notifications require gateway/service tokens and have no
published host ports.

The `marketplace` and `notifications` databases are separate databases on one
local Postgres container. Identity, Orders, Admin, and Catalog APIs currently
reuse marketplace handlers and the `marketplace` database to preserve existing
workflows. This is an API/process separation, not independent code or data
ownership; the shared database remains a scaling bottleneck. Moving users,
sessions, orders, and order items into service-owned databases requires the
next migration phase, including replacing cross-domain joins with explicit
service contracts and verifying the migration against a backup.

Set `ADMIN_SETUP_TOKEN` in the existing `.env`, then start the stack:

```powershell
docker compose up --build
```

Open `http://localhost:8000`. Internal APIs are only reachable on the Compose
network; requests use the service tokens in `.env.example`. Those tokens are
development-only; replace them before sharing a stack. Set the Gmail SMTP
values in `.env` for delivery. The outbox retries temporary
failures up to five attempts and removes message content after successful or
final delivery. SMTP delivery is at-least-once; recovery after a worker crash
can result in a duplicate message.

This Compose setup is a local multi-process migration; its service processes
still share the marketplace database. Render Free cannot run this topology
reliably: its free web services spin down, cannot receive private-network
requests, it allows one Free Postgres database per workspace, and it blocks
outbound SMTP ports 465/587.

The Render Blueprint uses one web service instead. When a service URL such as
`CATALOG_SERVICE_URL` is unset, the gateway calls that API in-process; the
gateway and internal API still validate a service token, generated at startup
if it is not configured. This avoids depending on Render private networking or
additional services on the Free plan. Keep service URLs unset for this
single-service deployment.

## Cloud prototype preparation

The Firebase Hosting configuration serves the built React app and rewrites
`/api/**` to a Cloud Run service named `liparahisi-api` in
`africa-south1`. The Docker image accepts `APP_MODULE` so the same image can
run the gateway or one of the internal FastAPI services. Build the frontend
with `npm run build` before deploying Hosting.

No cloud resources have been created. The Firebase/GCP project ID and domain
are not available yet, so deployment and custom-domain verification cannot
proceed. The selected no-budget prototype uses Neon Free for PostgreSQL instead
of Cloud SQL. Neon Free suspends compute after inactivity and has limited
storage/compute; it is for a prototype, not a high-availability production
payment system. Cloud Run has a free usage allowance but requires billing and
can charge if usage exceeds that allowance. Set spending alerts and keep live
payments disabled. Do not assume alerts prevent charges.

Keep `DATABASE_URL`, `NOTIFICATIONS_DATABASE_URL`, service tokens, SMTP
credentials, and `ADMIN_SETUP_TOKEN` in Google Secret Manager, not in source
control or Firebase Hosting files. The existing database is not copied or
changed by this preparation. Identity, Orders, Admin, and Catalog still share
the marketplace database, so this topology does not yet provide independent
data ownership or production-grade scaling.

Cloudflare can manage the domain's DNS; use the DNS records Firebase Hosting
provides for the custom domain and complete HTTPS verification first. Firebase
Hosting already serves the frontend through its CDN. If Cloudflare proxying is
enabled later, bypass caching for `/api/**` (especially login, checkout, and
admin routes) and verify cookies and HTTPS before routing users through it.

## Render deployment

The Blueprint uses Render's Free Postgres plan to start without a database
charge. It is limited to 1 GB, has no backups, and expires 30 days after
creation. Render gives a 14-day grace period to upgrade after expiration; after
that it deletes the database and its data. Use it for testing only, and export
or upgrade before the expiration date if you need to keep records. The web
service remains in demo-payment mode.

1. Push the project to GitHub and open Render's **New > Blueprint** flow.
2. Select the repository and review the resources and their current prices.
3. Apply the Blueprint only after accepting the displayed database charge.
4. In the web service's Environment page, reveal `ADMIN_SETUP_TOKEN` and use
   it once to create the main administrator. Do not share that token.
5. Sellers register from the app; the administrator approves each seller.

The service's `DATABASE_URL` is connected to the managed Postgres resource.
The QR contains a public product URL at `PUBLIC_APP_URL`; scanning it opens
the product name, price, and available checkout methods. It does not contain
payment credentials.

Render Free blocks outbound SMTP ports, so use Resend's HTTPS API for account
verification and password recovery:

1. Verify a sender domain in Resend and choose a sender address on that domain.
2. In Render's Environment page, set `EMAIL_PROVIDER=resend`, add `RESEND_API_KEY`
   as a secret, and set `EMAIL_FROM` to the verified sender address.
3. Check `GET /api/config/integrations`; `email_configured` should be `true`.

Do not put the Resend API key in source control or chat. SMTP remains available
for local environments that permit outbound SMTP. The Render Blueprint keeps
`DEMO_MODE=true` and live payments disabled.

The Free Postgres database in this Blueprint is for testing only: it does not
provide durable backups and expires. Do not use it for production records or
enable live payments until moving to a database plan with backups and testing
restores. `DEMO_MODE` remains enabled, and Selcom split settlement, verified
payment callbacks, and production reconciliation still require Selcom's
confirmed integration specifications and merchant credentials.

## Verify

```powershell
python -m unittest discover -s . -p "test*.py"
npm run build
```
