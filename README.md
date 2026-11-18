# LipaRahisi

React + Python prototype for product QR codes and Selcom-hosted checkout in
Tanzania. A buyer scans the QR, opens the product page, enters their contact
details, then pays on Selcom's checkout page. Payment status is checked through
Selcom's authenticated order-status API; returning to the site by itself is not
treated as proof of payment.

## What you need

- Python 3.10 or later
- Node.js and npm
- Selcom sandbox API URL, API key, API secret, and vendor ID
- A public app URL for QR scans. For phone testing on the same Wi-Fi, use your
  computer's LAN IP; for real buyers, deploy with HTTPS.

Selcom only displays payment channels enabled for your merchant account.
M-Pesa, Tigo Pesa/Mixx by Yas, and Airtel Money must be enabled in the Selcom
sandbox account before testing those channels.

## Configure

For a complete local demo, no payment credentials are needed. The app defaults
to `DEMO_MODE=true`, so a buyer can scan a QR, open checkout, and confirm a
demo payment locally. No real money moves and no payment gateway is called.

In PowerShell, from the project folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set:

- `SELCOM_BASE_URL`: the exact HTTPS sandbox API URL supplied by Selcom
- `SELCOM_API_KEY`, `SELCOM_API_SECRET`, `SELCOM_VENDOR`: values from the
  Selcom sandbox merchant account
- `MERCHANT_ADMIN_TOKEN`: a random token with at least 32 characters for
  protecting product creation outside demo mode
- `PUBLIC_APP_URL`: the URL buyers' phones can open (no trailing slash)

Do not commit `.env` or put Selcom credentials in the React app.

## Run locally

Start the Python API in one PowerShell window:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Start the React site in a second window:

```powershell
npm install
npm run dev -- --host 0.0.0.0
```

Open the Vite address, enter the merchant token and product details, then
download or share the QR. For a phone on the same Wi-Fi, set `PUBLIC_APP_URL`
to the computer's LAN URL shown by Vite (for example
`http://192.168.1.20:5173`). The phone must be able to reach both the Vite app
and its API proxy. For real customers, deploy the frontend and API on a public
HTTPS domain and set that domain as `PUBLIC_APP_URL`.

The QR is a **QR code**, not a one-dimensional barcode. It contains a link to
the product page, not payment credentials or a claim that payment succeeded.
The app stores products and orders in `backend/data.sqlite3`. This is a
single-merchant starter; before production, deploy with HTTPS, back up the
database, and add merchant accounts and access controls appropriate to the
business.

## Deploy on Render

This project includes `Dockerfile` and `render.yaml`. It does not contain a
public deployment URL because that URL is created only after you authorize a
deployment account and Render generates the service hostname.

1. Push this project to GitHub.
2. Open Render and choose **New > Blueprint**.
3. Select the GitHub repository.
4. Render builds the React frontend and Python backend from `Dockerfile`.
5. After deployment, open `https://your-service.onrender.com`.
6. Set `PUBLIC_APP_URL` in Render to that exact URL only if it differs from
   the automatically detected request URL. QR links are generated from HTTPS
   request origins.

The free service sleeps after inactivity, data in `/tmp` is ephemeral, and
demo/testing use is intended only for prototype access. For real payment
records and products, add a persistent PostgreSQL/MySQL database instead of
SQLite.

## Verify

Run the signing and payment-link unit tests:

```powershell
python -m unittest backend.test_selcom
```

Selcom API reference: <https://developers.selcommobile.com/>
