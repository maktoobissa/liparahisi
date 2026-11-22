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
- Product variants with separate stock counts; each variant uses its product's
  base price and can be selected in single-item or cart checkout.
- Seller-uploaded MP4/WebM product videos during product creation or from its
  product card (up to 20 MiB each, three per product), with playback,
  add-to-cart, and direct checkout from the buyer feed.
- Seller-managed delivery status, carrier, tracking code, and a separately
  collected delivery fee shown to buyers (not included in the product payment).
- Buyer cancellation requests before shipment and return requests within seven
  days after delivery, with administrator resolution. Return approval records
  a decision only; no payment is refunded by the system.
- Seller payout ledger that reconciles completed-order earnings against
  admin-recorded partial payments; entries document external transfers and
  never initiate one.
- Admin-only system alerts for failed payment and email delivery attempts.
- Admin-only, unpublished drafts for terms, privacy, and cancellation/return
  policies; these require business details and legal review before publication.
- Optional abandoned-checkout recovery: buyers can opt in to one reminder email
  after 24 hours, with an unsubscribe link. Only unpaid, expired checkouts are
  eligible; the email starts a new checkout because the old one has expired.
- Verified-order reviews subject to administrator moderation.
- Buyer support tickets with administrator responses.
- Admin-managed marketplace-wide percentage or fixed-value coupons, limited to
  demo checkout and one completed redemption per buyer.
- Downloadable receipt PDFs with item subtotals, total charged, commission per
  seller, and seller payouts.
- Swahili, English, and French interface language selection, plus in-app
  pop-up notifications with an unread counter, optional sound, and browser
  notifications.
- Installable PWA with a branded loading screen and offline-cached application
  shell. Marketplace data and checkout still require the API to be online.

Uploaded product images and videos are stored in the marketplace database and
served through public catalog endpoints; storefront images still use HTTP/HTTPS
URLs. Videos are stored as PostgreSQL `BYTEA` (or SQLite `BLOB` locally), so
their combined size counts against database storage; keep the configured
database capacity and backups in mind when sellers upload videos.
The interface supports all three languages; text without a translation entry
falls back to its source language. Seller-entered product/store names and
descriptions remain in the language provided by the seller and are not
automatically translated.
Seller sales/dashboard data refreshes automatically while the page is visible,
and pending order/checkout status is rechecked periodically. Browser
notifications require the visitor's permission; sound can be muted in the app.
The in-app notification list and unread counter are available while the page is
open. Push delivery while the app is closed is not configured. Approved
cancellation is allowed only before shipment and restores stock only in demo
mode; it does not issue a real payment refund. Return requests are accepted
within seven days of delivery, but approval does not initiate a refund.
M-Pesa and Halo Pesa can be selected in demo checkout, but neither is connected
to a live payment provider. Coupons are rejected when live payments are
enabled. Abandoned-checkout reminders require buyer opt-in and an available
email provider; Render Free service sleep or email-provider outages can delay
delivery beyond 24 hours. Payment/email failures are recorded in the admin
alerts panel, and an email alert is attempted for active administrators.

## Security and operations

- Account owners can enable optional TOTP two-factor authentication in account
  settings. Ten one-time recovery codes are shown once when enabling 2FA.
  Configure `MFA_ENCRYPTION_KEY` before enabling it; generate a Fernet key with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
  and keep it in the server's secret manager. Back up this key securely: losing
  it makes stored authenticator secrets unreadable.
- API requests are rate limited by client IP and route group, in addition to
  account-based login lockouts. Important sign-ins, security changes,
  moderation, seller changes, uploads, and sales actions are recorded in the
  administrator Audit view.
- Sellers can only access their own seller data, documents, and product
  uploads. Product images accept JPEG/PNG/WebP (5 MiB); private seller and
  product documents accept PDF/JPEG/PNG/WebP (10 MiB) and are checked by file
  signature as well as declared MIME type.
- Account settings list active sessions and can revoke all other devices.
- Security Intelligence currently uses explainable `LOCAL_RULES_V1` signals from
  accounts with five or more failed logins/current lockouts, repeated failed
  logins from one IP in the last 24 hours, seller paid orders left unfulfilled
  for over 48 hours, and cancellation requests in the last 90 days. These are
  review indicators,
  not proof of fraud; incomplete listings, low ratings, and KYC status are not
  treated as fraud evidence. An administrator must review and explicitly choose
  warnings, review, suspension, restoration, false-positive, or resolution
  actions. Suspended users can sign in only to view their alerts and submit an
  appeal. Admin routes are `/api/admin/security/scan`,
  `/api/admin/security/alerts`, and `/api/admin/security/appeals`; users access
  `/api/security/alerts` and submit appeals through
  `/api/security/alerts/{alert_id}/appeal`.
- Optional AI enrichment uses OpenAI's Responses API (`OPENAI_API_KEY`;
  `OPENAI_FRAUD_MODEL` defaults to `gpt-4o-mini`). When configured, each manual
  scan sends at most 20 alerts' categories, rule scores, and allowlisted
  aggregated counts; account IDs, names, email addresses, IP addresses, and
  user-written text are excluded. Requests set `store=false`. The AI score and
  validated evidence-linked reason codes are displayed separately from the
  rule score; the model cannot warn, suspend, or otherwise act on an account.
  Without the key, the rules still run and alerts report AI as disabled.
- Fake-review coordination, account-takeover signals, and unusual API activity
  are not inferred until suitable event data and validated rules/models exist.
- The seller business assistant answers supported questions from tenant- and
  branch-scoped records. Revenue and profit include only completed payments
  with a provider reference; demo and sandbox orders are excluded. Optional
  recommendation enrichment uses `BUSINESS_AI_PROVIDER=openai` and
  `OPENAI_BUSINESS_MODEL` (default `gpt-4o-mini`); only aggregate counts and
  allowlisted recommendation codes are sent to the provider. Without a key,
  verified metrics remain available and AI-generated recommendations are
  clearly marked as unconfigured. Users can save English, Kiswahili, or French
  as an account language; notification templates use that preference.
- The admin dashboard includes database/cache health and in-process 5xx
  counters. Server exceptions are also logged to the application logs.
- When S3 settings are present, the API uploads a database snapshot on startup
  and then every 24 hours, with server-side AES-256 encryption requested.
  Configure a private S3-compatible bucket, a scoped access key, lifecycle
  retention, and (for PostgreSQL) a working `pg_dump`. The worker runs in the
  API process; for a production deployment that can scale to multiple
  instances or sleep, schedule `python -m backend.backup` in a dedicated
  always-on scheduler instead. The backup page shows whether storage is
  configured and the latest successful upload in the current process.
- API regression tests run with `python -m unittest discover`; focused
  frontend API and upload-validation tests run with `npm run test:frontend`.

## Payments

The app supports ClickPesa Mobile Money USSD Push for a single product or a
multi-seller cart. ClickPesa collects the total into the platform's merchant
account. The app records the 5% platform commission and each seller's payout
in its ledger, but does **not** transfer money to sellers; the platform must
pay them manually and reconcile those payouts.

Abliner is the selected provider for a future automatic seller-payout workflow,
but automatic transfers are not implemented or enabled. The current ledger is
manual; do not treat a recorded payout as proof of a provider transfer.

Demo mode remains the default (`DEMO_MODE=true`), and the Render Blueprint
keeps ClickPesa and Abliner live payments disabled. A successful demo checkout
transfers no money.
Never ask buyers for their wallet PIN or OTP.

ClickPesa requires merchant KYC and API access. Its public docs state that API
calls are made against production (there is no sandbox), so integration tests
mock the API and do not initiate real charges. Enable live payments only after
the merchant account, payment methods, webhook checksum, durable database, and
manual seller-payout process are ready:

1. Register with ClickPesa, complete KYC, and create an API application with
   the Payment API feature.
2. In Render's Environment page, set `CLICKPESA_CLIENT_ID`,
   `CLICKPESA_API_KEY`, and `CLICKPESA_CHECKSUM_KEY` as secrets. Never put them
   in source control or chat.
3. Configure application webhooks for `PAYMENT RECEIVED` and `PAYMENT FAILED`
   to `https://<your-app-domain>/api/webhooks/clickpesa`, and enable checksum
   validation using the same checksum key.
4. Ensure the app uses a durable production database, then set
   `CLICKPESA_MANUAL_PAYOUTS_CONFIRMED=true`, `CLICKPESA_LIVE_ENABLED=true`,
   and `DEMO_MODE=false`. Keep live disabled until those conditions are met.

ClickPesa's published [pricing](https://clickpesa.com/payment-gateway/pricing/)
lists free account creation up to a total transaction limit of TZS 100,000,
KYC search/onboarding at TZS 25,000, and M-Pesa channel setup/activation at
TZS 250,000. Mobile-money USSD Push fees are charged to the customer in
addition to mobile-network fees; the exact fee should be confirmed in the
ClickPesa payment flow. Fees and terms can change; confirm them with ClickPesa
before accepting live payments.

### PesaPal Sandbox

PesaPal API 3 is integrated in **sandbox mode only**. It uses the official
sandbox endpoint and cannot collect real money. Live PesaPal payments are not
implemented or enabled. Sandbox checkouts are visibly labelled, do not reduce
product stock, send payment emails, or count as seller/admin sales.

To try it on Render:

1. Keep `DEMO_MODE=true` and `PESAPAL_SANDBOX_ENABLED=false` until setup is
   complete. Obtain the public test credentials from PesaPal's
   [official API 3 demo keys](https://developer.pesapal.com/api3-demo-keys.txt);
   choose the Tanzania merchant credentials. Do not use production credentials.
2. Register `https://liparahisi.onrender.com/api/webhooks/pesapal` as an IPN
   using `GET` in PesaPal's
   [sandbox registration form](https://cybqa.pesapal.com/PesapalIframe/PesapalIframe3/IpnRegistration).
   Copy the returned IPN ID.
3. In Render's Environment page, set `PESAPAL_CONSUMER_KEY`,
   `PESAPAL_CONSUMER_SECRET`, and `PESAPAL_NOTIFICATION_ID` to the sandbox
   values. Then set `PESAPAL_SANDBOX_ENABLED=true`. Keep all ClickPesa live
   flags false.

The app redirects buyers to PesaPal's hosted sandbox page. Both the IPN and
browser callback are verified server-to-server with `GetTransactionStatus`;
the browser's return alone never marks an order paid. If the callback is not
reachable, confirm that the public Render URL matches the registered IPN and
that the sandbox variables are set. Never put credentials in source control,
the browser, or chat. Verify fees, merchant onboarding, and production
credentials with PesaPal before requesting a separate live integration.

### Abliner Mobile Money
The app also supports Abliner Mobile Money deposits for product and cart
checkout. Abliner's [official API documentation](https://abliner.net/docs)
specifies a production-only API (no sandbox), signed deposit requests, and
signed webhooks. Automated tests mock the API and never start real charges.
The checkout sends the HTTPS callback URL
`https://<your-app-domain>/api/webhooks/abliner` and uses an idempotency key
per checkout. Abliner's published deposit range for TZS is TZS 500 to
TZS 3,000,000 per transaction.

To prepare Abliner without exposing credentials:

1. Complete Abliner's business verification and generate both an API key and a
   separate webhook signing secret in its dashboard. An API key alone is not
   enough for deposits.
2. Set `ABLINER_API_KEY` and `ABLINER_WEBHOOK_SECRET` as secrets in Render.
   Never place either value in source control, frontend code, or chat.
3. Keep `ABLINER_LIVE_ENABLED=false` until the account and callback are ready,
   seller payouts are understood, and the app has a durable production
   database with backups. The current Free Render database in this Blueprint
   is for testing and must not be used for live financial records.
4. Only after those requirements are met, set `ABLINER_LIVE_ENABLED=true`.
   Abliner then sends a real mobile-money prompt; the API does not provide a
   sandbox. `DEMO_MODE` can remain enabled for other payment methods.

The current Selcom helper remains unused; setting Selcom credentials does not
enable live payments.

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
To test 2FA, add a Fernet key as `MFA_ENCRYPTION_KEY`. S3 backups are enabled
only when `BACKUP_S3_BUCKET`, `AWS_ACCESS_KEY_ID`, and
`AWS_SECRET_ACCESS_KEY` are configured; set `BACKUP_S3_ENDPOINT_URL` for an
S3-compatible provider.

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

The Blueprint keeps the web service and the existing Postgres database on the
Free plan. It does not create a paid Key Value instance or request additional
database storage. The existing database currently has 1 GB of storage. The web
service remains in demo-payment mode.

1. Push the project to GitHub and open the existing Render Blueprint.
2. Confirm that syncing does not add paid resources before applying it.
3. In the web service's Environment page, reveal `ADMIN_SETUP_TOKEN` and use
   it once to create the main administrator. Do not share that token.
4. Sellers register from the app; the administrator approves each seller.

The service's `DATABASE_URL` is connected to the managed Postgres resource.
The API reuses PostgreSQL connections through a per-process pool capped at
five connections, and serves Vite's fingerprinted `/assets` files with
long-lived browser caching. Redis product caching is optional and only runs
when `REDIS_HOST` and `REDIS_PORT` are configured; they are intentionally
unset on the no-cost deployment. Without Redis, product requests continue to
work directly against Postgres. Pooling and static-asset caching do not remove
Free web-service cold starts or establish capacity for 10,000 concurrent
users; that requires a separately provisioned staging environment and load
testing against production-like infrastructure.
The QR contains a public product URL at `PUBLIC_APP_URL`; scanning it opens
the product name, price, and available checkout methods. It does not contain
payment credentials.

Render Free blocks outbound SMTP ports, so the FastAPI backend sends mail
through Resend's official HTTPS email API. It sends verification and welcome
messages, password-reset links, password-change alerts, payment-failure
notices and receipts to buyers, and sale-completion notices to sellers.
Emails have HTML and plain-text versions.

Configure each Render web service in **Environment**:

1. In Resend, verify a domain you own and choose a sender on that domain, such
   as `LipaRahisi <no-reply@mail.your-domain.tld>`. The shared
   `onboarding@resend.dev` sender is for limited testing, not production users.
2. Set `EMAIL_PROVIDER=resend`, add `RESEND_API_KEY` as a secret, and set
   `EMAIL_FROM` to the verified sender. Never put API keys in source control
   or chat; rotate any key that has been exposed.
3. Set `FRONTEND_URL` to that service's public HTTPS origin with no trailing
   slash. For the existing `liparahisi.onrender.com` service, use
   `https://liparahisi.onrender.com`. Set `BACKEND_URL` to the same origin:
   this deployment serves the React frontend and FastAPI API from one origin,
   and the frontend uses relative `/api` paths. `BACKEND_URL` is reserved for
   a future split deployment; email links use `FRONTEND_URL`.
4. Save the environment and redeploy. Check
   `GET /api/config/integrations`; `email_configured` should be `true`.

`email_configured` confirms only that the required settings are present; it
does not verify the sender domain or prove that a recipient will accept mail.
`EMAIL_FROM` is the sender address. Account verification/reset messages go to
that account's registered address, buyer receipts go to the buyer's registered
address when signed in (or the checkout address for guests), and sale notices
go to the seller's registered address. Verify the sender domain in Resend to
deliver to other recipients; do not send private order details to every account.

The second Render service, if used, must use its own public origin for both
URL variables. `PUBLIC_APP_URL` remains as a backward-compatible fallback for
older configurations. The forgot-password endpoint responds with the same
message whether an account exists or not, and rate-limits reset email delivery
to one request per account every 15 minutes. Delivery logs record event type
and outcome only; they do not include recipient addresses, email bodies, or
reset tokens. SMTP remains available for local environments that permit
outbound SMTP. The Render Blueprint keeps `DEMO_MODE=true` and live payments
disabled.

The Free Postgres database in this Blueprint is for testing only: it does not
provide durable backups and expires. Do not use it for production records or
enable live payments until moving to a database plan with backups and testing
restores. `DEMO_MODE` remains enabled and ClickPesa live payments remain disabled in the
Blueprint. The Free Postgres database is not suitable for live financial
records; move to durable production storage with backups before enabling
payments. The app does not perform seller payouts.

## Verify

```powershell
python -m unittest discover -s . -p "test*.py"
npm run build
```
