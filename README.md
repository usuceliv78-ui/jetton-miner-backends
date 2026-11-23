STORM payment verification backend (initial scaffold)
Deploy
Upload the contents of this ZIP to the root of your separate private GitHub repository.
Import that repository into Vercel as a new project.
Add the environment variables below in Vercel → Settings → Environment Variables.
Redeploy after adding them.
Environment variables
MINI_APP_BOT_TOKEN: Telegram bot token used to validate Mini App requests (secret).
ADMIN_BOT_TOKEN: Telegram bot token used for admin notifications and webhook actions (secret).
ADMIN_TELEGRAM_ID: Telegram numeric user ID allowed to administer payment decisions.
TELEGRAM_WEBHOOK_SECRET: secret token used to validate Telegram webhook requests.
TONAPI_KEY: TONAPI API key.
SUPABASE_URL: https://fntgxukftaumztacprwe.supabase.co
SUPABASE_SECRET_KEY: your Supabase server-side secret key (sb_secret_...).
PAYMENT_WALLET: UQBnmmqia2AC2X3oDa9jptT9fSq9u88hYgDw5r9YkmCXiKn3 (matches the companion frontend; override if needed).
FRONTEND_ORIGIN: exact frontend deployment origin, with no trailing slash.
Project TON / JETTON settings
These values match the previously updated JETTON frontend:
Receiving TON wallet: UQBnmmqia2AC2X3oDa9jptT9fSq9u88hYgDw5r9YkmCXiKn3
STORM/JETTON token master (contract): EQAQXlWJvGbbFfE8F3oS8s87lIgdovS455IsWFaRdmJetTon
The payment-verification API checks native TON transfers to PAYMENT_WALLET. The JETTON master address is documented for the token/frontend; it is not used to verify these TON miner purchases.
Miner payment amounts
Payment type
Amount
Verification
0.5 TON
Basic Miner
1.5 TON
Advanced Miner
3 TON
Pro Miner
5 TON
Ultra Miner
10 TON
Legendary Miner
25 TON
Test
Open https://YOUR-BACKEND.vercel.app/health. Expected: {"ok":true,"service":"storm-payment-verification"}
Important limitations
This is an initial scaffold, not a production-ready payment-crediting system.
Destination matching is exact. TONAPI may return a raw address while your configured wallet is friendly-format, so a valid payment may be rejected until address normalization is added and tested.
A shared receiving wallet plus transaction hash does not prove which Telegram user made the payment. Use unique memo/order attribution or authenticated wallet ownership before production.
This endpoint records a verified claim only. It does not activate miners or credit STORM.
Do not use mainnet payments for testing until end-to-end checks are complete. Never put secrets in frontend HTML or GitHub.
