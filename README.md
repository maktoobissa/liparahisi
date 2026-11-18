# LipaRahisi

LipaRahisi is a Swahili-first multi-seller marketplace prototype. Sellers can
register, wait for the main administrator's approval, manage products and view
their sales. Buyers can scan a product QR, submit checkout details, and receive
a demo receipt. The main administrator approves sellers and sees marketplace
sales and the 5% platform commission.

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
The QR contains a product URL; it does not contain payment credentials.

## Verify

```powershell
python -m unittest backend.test_selcom backend.test_marketplace
npm run build
```
