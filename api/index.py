
import os
import asyncio
import re
import hmac
import hashlib
import json
import time
import base64
import uuid
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("storm-payments")
app = FastAPI(title="STORM Payment Verification API")

# ------------------------------------------------------------- CONFIG ---

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "").rstrip("/")
MINI_APP_BOT_TOKEN = os.getenv("MINI_APP_BOT_TOKEN", "")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
TONAPI_KEY = os.getenv("TONAPI_KEY", "")
TONAPI_BASE_URL = os.getenv(
    "TONAPI_BASE_URL", "https://tonapi.io"
).rstrip("/")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
# Default matches the receiving wallet in the companion JETTON frontend.
# Override PAYMENT_WALLET in Vercel Environment Variables when changing wallets.
PAYMENT_WALLET = os.getenv(
    "PAYMENT_WALLET",
    "UQBnmmqia2AC2X3oDa9jptT9fSq9u88hYgDw5r9YkmCXiKn3",
).strip()

PAYMENT_WINDOW_SECONDS = 240
TONAPI_TIMEOUT = 8
SUPABASE_TIMEOUT = 5

PAYMENTS = {
    "verification": 500_000_000,
    "basic": 1_500_000_000,
    "advanced": 3_000_000_000,
    "pro": 5_000_000_000,
    "ultra": 10_000_000_000,
    "legendary": 25_000_000_000,
}

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
B64URL = re.compile(r"^[A-Za-z0-9_-]{43}={0,1}$")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN else [],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ------------------------------------------------------- ERROR HANDLING ---

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        {"ok": False, "error": exc.detail, "detail": exc.detail},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    fields = [
        {
            "loc": [str(x) for x in e.get("loc", [])],
            "msg": e.get("msg", ""),
        }
        for e in exc.errors()
    ]
    return JSONResponse(
        {
            "ok": False,
            "error": "Invalid request body",
            "detail": "Invalid request body",
            "fields": fields,
        },
        status_code=422,
    )


# ------------------------------------------------------------- MODELS ---

class CreatePaymentRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)
    payment_type: str = Field(min_length=1, max_length=32)


class VerifyRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)
    tx_hash: str = Field(min_length=8, max_length=512)
    payment_request_id: uuid.UUID


class PaymentStatusRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


class CreateWithdrawalRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)
    amount: float = Field(ge=3000, le=1000000000)
    wallet_address: str = Field(min_length=24, max_length=128)


# ---------------------------------------------------------- TELEGRAM ---

def validate_init_data(raw: str) -> str:
    if not MINI_APP_BOT_TOKEN:
        raise HTTPException(500, "MINI_APP_BOT_TOKEN is not configured")

    try:
        values = dict(parse_qsl(raw, keep_blank_values=True))
        received_hash = values.pop("hash")
        auth_date = int(values.get("auth_date", "0"))
    except Exception:
        raise HTTPException(401, "Invalid Telegram initData")

    if auth_date <= 0 or time.time() - auth_date > 86400:
        raise HTTPException(401, "Telegram initData expired")

    check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(values.items())
    )

    secret = hmac.new(
        b"WebAppData",
        MINI_APP_BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    expected = hmac.new(
        secret,
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "Telegram initData signature is invalid")

    try:
        return str(int(json.loads(values["user"])["id"]))
    except Exception:
        raise HTTPException(401, "Telegram user data missing")


# ----------------------------------------------------------- SUPABASE ---

def _sb_headers(prefer):
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise HTTPException(
            500, "Supabase environment variables are missing"
        )

    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    if prefer:
        headers["Prefer"] = prefer

    return headers


async def sb(method, path, *, params=None, body=None, prefer="return=representation"):
    headers = _sb_headers(prefer)

    try:
        async with httpx.AsyncClient(timeout=SUPABASE_TIMEOUT) as client:
            return await client.request(
                method,
                f"{SUPABASE_URL}/rest/v1/{path}",
                headers=headers,
                params=params,
                json=body,
            )
    except httpx.HTTPError:
        raise HTTPException(
            503, "Database temporarily unavailable, try again"
        )


def utcnow():
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00").replace(" ", "T")
    m = re.match(r"^(.*?)(\.\d+)?([+-]\d\d)(?::?(\d\d))?$", s)

    if not m:
        raise ValueError(f"bad timestamp {s!r}")

    base, frac, tzh, tzm = m.groups()
    frac = (frac or ".0")[:7].ljust(7, "0")

    return datetime.fromisoformat(
        f"{base}{frac}{tzh}:{tzm or '00'}"
    ).astimezone(timezone.utc)


async def get_payment_request(request_id: str):
    r = await sb(
        "GET",
        "payment_requests",
        params={
            "id": f"eq.{request_id}",
            "select": "*",
            "limit": "1",
        },
    )

    if r.status_code != 200:
        raise HTTPException(502, "Could not read payment request")

    rows = r.json()
    return rows[0] if rows else None


async def set_request_status(request_id: str, new_status: str):
    try:
        await sb(
            "PATCH",
            "payment_requests",
            params={
                "id": f"eq.{request_id}",
                "status": "eq.pending",
            },
            body={"status": new_status},
            prefer="return=minimal",
        )
    except HTTPException:
        pass


# --------------------------------------------------------- TON ADDRESS ---

def _crc16_xmodem(data: bytes) -> int:
    crc = 0

    for b in data:
        crc ^= b << 8

        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x1021)
                if crc & 0x8000
                else (crc << 1)
            )
            crc &= 0xFFFF

    return crc


def parse_ton_address(addr):
    if not isinstance(addr, str):
        raise ValueError("address is not a string")

    addr = addr.strip()

    if ":" in addr:
        wc, h = addr.split(":", 1)

        if not HEX64.match(h):
            raise ValueError("bad raw address")

        return int(wc), bytes.fromhex(h)

    if len(addr) != 48:
        raise ValueError("bad friendly address length")

    raw = base64.urlsafe_b64decode(
        addr.replace("+", "-").replace("/", "_")
    )

    if (
        len(raw) != 36
        or _crc16_xmodem(raw[:34]) != int.from_bytes(raw[34:], "big")
    ):
        raise ValueError("bad friendly address checksum")

    if (raw[0] & 0x7F) not in (0x11, 0x51):
        raise ValueError("bad friendly address tag")

    return (
        int.from_bytes(raw[1:2], "big", signed=True),
        raw[2:34],
    )


def same_address(a, b) -> bool:
    try:
        return parse_ton_address(a) == parse_ton_address(b)
    except Exception:
        return False


def addr_str(field):
    if isinstance(field, dict):
        return field.get("address")

    return field if isinstance(field, str) else None


# --------------------------------------------------------- TONAPI TRACE ---

async def fetch_trace(tx_hash: str) -> dict:
    if not TONAPI_KEY:
        raise HTTPException(500, "TONAPI_KEY is not configured")

    try:
        async with httpx.AsyncClient(timeout=TONAPI_TIMEOUT) as client:
            r = await client.get(
                f"{TONAPI_BASE_URL}/v2/traces/{tx_hash}",
                headers={"Authorization": f"Bearer {TONAPI_KEY}"},
            )
    except httpx.HTTPError:
        raise HTTPException(
            503, "TON API unreachable, try again shortly"
        )

    if r.status_code == 404:
        raise HTTPException(
            404, "Transaction trace not found yet. Retry in a few seconds."
        )

    if r.status_code == 429:
        raise HTTPException(
            503,
            "TON API rate limit reached, retry shortly",
            headers={"Retry-After": "5"},
        )

    if r.status_code != 200:
        raise HTTPException(
            502, "TON transaction trace lookup failed"
        )

    data = r.json()

    if not isinstance(data, dict):
        raise HTTPException(
            502, "TON API returned an unexpected trace response"
        )

    return data


def walk_dicts(value):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_dicts(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def tx_candidates(trace: dict):
    seen = set()

    for item in walk_dicts(trace):
        if (
            isinstance(item.get("in_msg"), dict)
            and item.get("utime") is not None
            and item.get("hash")
        ):
            h = str(item.get("hash")).lower()

            if h not in seen:
                seen.add(h)
                yield item


def normalize_tx_hash(raw: str) -> str:
    h = raw.strip()

    if "://" in h:
        h = (
            h.split("?")[0]
            .split("#")[0]
            .rstrip("/")
            .split("/")[-1]
        )

    if HEX64.match(h) or B64URL.match(h):
        return h

    raise HTTPException(
        400,
        "Invalid transaction hash (expected 64-character hex hash or explorer link)",
    )



# ------------------------------------------------------- TELEGRAM BOT API ---

async def telegram_api(method: str, payload: dict):
    if not ADMIN_BOT_TOKEN:
        raise HTTPException(500, "ADMIN_BOT_TOKEN is not configured")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/{method}",
                json=payload,
            )
    except httpx.HTTPError:
        raise HTTPException(503, "Telegram service is temporarily unavailable")
    if response.status_code != 200 or not response.json().get("ok"):
        logger.error("Telegram %s failed: %s", method, response.text[:500])
        raise HTTPException(502, f"Telegram {method} failed")
    return response.json().get("result")


async def notify_admin(req: dict, username: str | None, tx_hash: str):
    if not ADMIN_TELEGRAM_ID:
        raise HTTPException(500, "ADMIN_TELEGRAM_ID is not configured")
    amount_ton = int(req["amount_nano"]) / 1_000_000_000
    username_text = f"@{username}" if username else "(not provided)"
    message = (
        "⚡ STORM PAYMENT REQUEST\n\n"
        f"User ID: {req['telegram_user_id']}\n"
        f"Username: {username_text}\n"
        f"Payment type: {req['payment_type']}\n"
        f"Amount: {amount_ton:g} TON\n"
        f"Transaction hash:\n<code>{tx_hash}</code>\n"
        f"Request ID: <code>{req['id']}</code>\n"
        f"Submitted: {iso_z(utcnow())}\n\n"
        "Check the payment in your wallet/explorer before approving."
    )
    return await telegram_api("sendMessage", {
        "chat_id": ADMIN_TELEGRAM_ID,
        "text": message,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Confirm", "callback_data": f"storm:approve:{req['id']}"},
                {"text": "❌ Reject", "callback_data": f"storm:reject:{req['id']}"},
            ]]
        },
    })


async def send_user_status(user_id: str, approved: bool, payment_type: str):
    status = "approved" if approved else "rejected"
    message = (
        f"✅ Your STORM {payment_type} payment has been approved."
        if approved else
        f"❌ Your STORM {payment_type} payment was rejected. Please contact support if you think this is a mistake."
    )
    try:
        await telegram_api("sendMessage", {"chat_id": user_id, "text": message})
    except HTTPException:
        logger.exception("Could not notify user %s about %s payment", user_id, status)


async def update_payment_status(request_id: str, status: str):
    result = await sb(
        "PATCH",
        "payment_requests",
        params={"id": f"eq.{request_id}", "status": "eq.pending"},
        body={"status": status},
        prefer="return=representation",
    )
    if result.status_code not in (200, 204):
        logger.error("Payment status update failed: %s %s", result.status_code, result.text[:300])
        raise HTTPException(502, "Could not update payment status")
    rows = result.json() if result.content else []
    return rows[0] if rows else None


async def get_withdrawal_request(request_id: str):
    r = await sb(
        "GET",
        "withdrawal_requests",
        params={"id": f"eq.{request_id}", "select": "*", "limit": "1"},
    )
    if r.status_code != 200:
        raise HTTPException(502, "Could not read withdrawal request")
    rows = r.json()
    return rows[0] if rows else None


async def notify_admin_withdrawal(req: dict):
    if not ADMIN_TELEGRAM_ID:
        raise HTTPException(500, "ADMIN_TELEGRAM_ID is not configured")
    username = f"@{req['telegram_username']}" if req.get("telegram_username") else "(not provided)"
    message = (
        "⚡ STORM WITHDRAWAL REQUEST\n\n"
        f"User ID: {req['telegram_user_id']}\n"
        f"Username: {username}\n"
        f"Requested: {float(req['gross_amount']):,.3f} STORM\n"
        f"Fee: {float(req['fee_amount']):,.3f} STORM\n"
        f"Net payout: {float(req['payout_amount']):,.3f} STORM\n"
        f"TON wallet: <code>{req['wallet_address']}</code>\n"
        f"Request ID: <code>{req['id']}</code>\n"
        f"Submitted: {iso_z(utcnow())}\n\n"
        "Verify the user's balance and miner eligibility. Send the net STORM payout manually, then press Confirm."
    )
    return await telegram_api("sendMessage", {
        "chat_id": ADMIN_TELEGRAM_ID,
        "text": message,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[
            {"text": "✅ Confirm", "callback_data": f"stormwd:approve:{req['id']}"},
            {"text": "❌ Reject", "callback_data": f"stormwd:reject:{req['id']}"},
        ]]},
    })


async def send_withdrawal_status(user_id: str, approved: bool, payout_amount: float):
    message = (
        f"✅ Your STORM withdrawal was approved. Net payout: {payout_amount:,.3f} STORM."
        if approved else
        "❌ Your STORM withdrawal was rejected. The requested amount has been returned to your in-app balance."
    )
    try:
        await telegram_api("sendMessage", {"chat_id": user_id, "text": message})
    except HTTPException:
        logger.exception("Could not notify user %s about withdrawal decision", user_id)


# ---------------------------------------------------------- ADMIN BROADCAST ---

# In-memory draft is intentionally short-lived; it is created and confirmed by
# the configured admin only. A redeploy clears any unconfirmed draft.
BROADCAST_DRAFT = None
BROADCAST_PAGE_SIZE = 1000


async def get_broadcast_recipients() -> list[str]:
    """Return unique Telegram IDs recorded in either request table."""
    recipients = set()
    for table in ("payment_requests", "withdrawal_requests"):
        offset = 0
        while True:
            result = await sb(
                "GET", table,
                params={
                    "select": "telegram_user_id",
                    "order": "telegram_user_id.asc",
                    "limit": str(BROADCAST_PAGE_SIZE),
                    "offset": str(offset),
                },
            )
            if result.status_code != 200:
                logger.error("Could not load broadcast recipients from %s: %s", table, result.text[:300])
                raise HTTPException(502, f"Could not load users from {table}")
            rows = result.json()
            for row in rows:
                value = row.get("telegram_user_id")
                if value is not None and str(value).strip():
                    recipients.add(str(value).strip())
            if len(rows) < BROADCAST_PAGE_SIZE:
                break
            offset += len(rows)
    recipients.discard(str(ADMIN_TELEGRAM_ID))
    return sorted(recipients)


async def copy_admin_message_to_user(client: httpx.AsyncClient, source_chat_id: str, message_id: int, user_id: str) -> bool:
    try:
        response = await client.post(
            f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/copyMessage",
            json={
                "chat_id": user_id,
                "from_chat_id": source_chat_id,
                "message_id": message_id,
            },
        )
        data = response.json()
        return response.status_code == 200 and bool(data.get("ok"))
    except Exception:
        logger.exception("Broadcast delivery failed for Telegram user %s", user_id)
        return False


async def run_broadcast(source_chat_id: str, message_id: int) -> tuple[int, int, int]:
    recipients = await get_broadcast_recipients()
    sent = failed = 0
    # Stay comfortably below Telegram's ordinary per-bot messaging limit.
    async with httpx.AsyncClient(timeout=15) as client:
        for index, user_id in enumerate(recipients):
            if await copy_admin_message_to_user(client, source_chat_id, message_id, user_id):
                sent += 1
            else:
                failed += 1
            if index + 1 < len(recipients):
                await asyncio.sleep(0.05)
    return len(recipients), sent, failed


# -------------------------------------------------------------- ENDPOINTS ---

@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "storm-payment-verification",
    }


@app.post("/create-payment")
async def create_payment(body: CreatePaymentRequest):
    if body.payment_type not in PAYMENTS:
        raise HTTPException(400, "Unknown payment type")

    user_id = validate_init_data(body.init_data)

    if not PAYMENT_WALLET:
        raise HTTPException(500, "PAYMENT_WALLET is not configured")

    try:
        parse_ton_address(PAYMENT_WALLET)
    except ValueError:
        raise HTTPException(
            500, "PAYMENT_WALLET is not a valid TON address"
        )

    now = utcnow().replace(microsecond=0)
    expires = now + timedelta(seconds=PAYMENT_WINDOW_SECONDS)

    r = await
