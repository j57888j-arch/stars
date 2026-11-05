# WeraqamBot V2.3 — Telegram Stars-first telecom service bot

Python 3.12, aiogram 3.31, FastAPI, PostgreSQL, Redis, SQLAlchemy async and Alembic.

> The bot is intended for lawful telecommunications services only. It does not collect or forward Telegram login OTP codes, passwords or 2FA secrets.

## What this version does

- Telegram user registration, profile, activity tracking and invite links.
- Countries and service catalogue. Customers only see active digital services with a valid positive Stars price; stock is shown and the purchase button is disabled when stock is empty. The checkout rechecks the same conditions server-side.
- Admin can create a service with a required Stars price, or repair older catalogue entries from **🧩 Xizmatlar** using the **⭐ Stars narxi** action or `/setstars SERVICE_ID AMOUNT`.
- Telegram Stars invoices (`XTR`) only: invoice payload, payer, amount and currency validation; pre-checkout and successful-payment handlers; duplicate payment protection; saved Telegram charge ID; admin refund via `refundStarPayment`; `/paysupport`.
- No card top-up flow, card details, manual payment, CLICK or wallet checkout is exposed. Remove old `PAYMENT_MODE`, `PAYMENT_CARD_*` and `CLICK_*` variables from Render Environment after upgrading.
- Internal referral reward: a new user who joins via a referral link receives `REFERRAL_BONUS_STARS` as an in-bot discount credit immediately. This is not a transfer of Stars to the user's Telegram account. The credit is automatically deducted from their next Stars invoice and is restored if an unpaid order expires or is cancelled.
- Promo-code percentage/fixed discounts, per-user/global limits and expiration.
- Individually tracked telecom resources: encrypted at rest, reserved atomically, and delivered only after an admin completes the paid order.
- Admin order queue: paid → processing when an admin claims it → completed. The customer receives a notification that the paid order entered the admin queue, another when an admin claims the order and starts processing, and a final message after completion.
- CRM lookup, block/unblock, customer order/payment history, notes and role/permission checks.
- Support tickets, admin ReplyKeyboard Support entry, `/tickets`, `/reply_ticket TICKET_ID answer`.
- Text/photo/video/document/animation broadcast through a database-backed outbox; optional URL button; rate-limited delivery and per-recipient statuses.
- `/broadcast_status ID` and `/outbox_status` to diagnose pending, sent and failed messages.
- CSV/XLSX/PDF date-range reports, dashboard, audit log.

## Telegram Stars rules

For digital goods/services sold inside Telegram, use Telegram Stars with currency `XTR`. The bot responds to `pre_checkout_query`, waits for `successful_payment` before marking the order paid or delivering the resource, persists `telegram_payment_charge_id`, supports `refundStarPayment`, and handles `/paysupport`. The bot never delivers a resource merely because pre-checkout was approved.

## Setup (local)

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set at least:

```env
BOT_TOKEN=your_bot_token
BOT_USERNAME=your_bot_username_without_at
ADMIN_IDS=123456789
DATABASE_URL=sqlite+aiosqlite:///./weraqambot.db
REDIS_URL=
ENVIRONMENT=development
TELEGRAM_STARS_ENABLED=true
REFERRAL_BONUS_STARS=10
```

Generate the encryption key if you use individually tracked inventory resources:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run migrations and start local polling:

```bash
alembic upgrade head
python -m app.main
```

For development, polling mode starts the bot and outbox worker together. Do not use polling and webhook simultaneously for one bot token.

## Render production deployment

The included `render.yaml` declares PostgreSQL, Redis/Key Value, a FastAPI Web Service and a Background Worker. Root Directory should be the project folder containing `Dockerfile`, `render.yaml` and `requirements.txt` (normally `weraqambot` if the repository contains the project as a subfolder). Runtime: Docker.

Web Service command:

```bash
alembic upgrade head && uvicorn app.api.server:app --host 0.0.0.0 --port $PORT
```

Background Worker command:

```bash
python -m app.workers.runner
```

Configure the same `BOT_TOKEN`, `DATABASE_URL`, `REDIS_URL`, `ADMIN_IDS`, `ADMIN_CHAT_ID` (optional), `PHONE_ENCRYPTION_KEY`, `TELEGRAM_STARS_ENABLED=true` and `REFERRAL_BONUS_STARS` on both services. Set `ENVIRONMENT=production`, `AUTO_CREATE_TABLES=false`, `WEBHOOK_URL=https://your-service.onrender.com`, and a strong `WEBHOOK_SECRET`.

The Web Service also runs an outbox consumer so support notifications and broadcasts are not silently left queued if the separate Worker is absent. The Worker can run alongside it; PostgreSQL `FOR UPDATE SKIP LOCKED` prevents both from claiming the same outbox row. Render's background-worker pattern is documented at https://render.com/docs/background-workers.

**Plan note:** the included Blueprint uses paid Starter/Basic resources for persistent Redis, PostgreSQL and a Background Worker. Render Free is not a reliable production setup for this bot: free PostgreSQL has a 30-day lifetime, Free Key Value is non-persistent, and a dedicated Background Worker is not available on the Free tier. Confirm current plan limits and prices in Render before deployment.

### Admin delivery requirement

Each admin in `ADMIN_IDS` must open the bot and press `/start` once before the bot can send them a private message. For reliable team notifications, add the bot to an admin supergroup and set `ADMIN_CHAT_ID` to that group's numeric chat ID (usually starts with `-100`). The bot must have permission to post messages there. Support tickets are also persisted and can always be viewed in the admin menu **💬 Support** or with `/tickets`.

### Health checks / UptimeRobot

- `GET /healthz` confirms the HTTP process is alive.
- `GET /readyz` checks database and Redis connectivity; configure UptimeRobot to monitor `https://your-service.onrender.com/readyz`.

Render may restart services after failed health checks; see https://render.com/docs/health-checks.

## Workflow

1. Put your own Telegram numeric ID into `ADMIN_IDS`.
2. Start the bot using `/start`, then open `/admin`.
3. Create a country.
4. Create a service: country → name → description → internal catalog value → initial stock → **Stars price**. The positive Stars price is mandatory; without it, a service is hidden from customer catalogue.
5. For existing services, open **🧩 Xizmatlar** → **⭐ Stars narxi**, or use `/setstars SERVICE_ID AMOUNT`.
6. Use **📦 Zaxira** for aggregate stock, or `/inventory_add SERVICE_ID RESOURCE` for encrypted individual resources. Keep `PHONE_ENCRYPTION_KEY` unchanged and backed up.
7. Customers select service → invoice → pay with Stars. Only after Telegram sends `successful_payment` does the order enter the paid admin queue.
8. Admin claims the order; customer is notified that work has started. Admin completes it; the customer receives the completion message and allocated resource (if individual inventory was used).

## Support and broadcast

- Customer submits **💬 Yordam**; the ticket is committed to PostgreSQL and notification events are written into the outbox in the same DB transaction.
- Admin receives the notification through `ADMIN_IDS` and/or `ADMIN_CHAT_ID`. If a private admin has not started the bot, Telegram will reject private delivery; use the group destination for team alerts.
- Admin can open **💬 Support**, use `/tickets`, and reply with `/reply_ticket TICKET_ID text`.
- For broadcast, open **📢 Xabar yuborish**, send text/photo/video/document/animation, then provide `-` to skip the URL button or enter a button label and URL. Delivery is possible only to users who previously started the bot; users who blocked the bot or removed access will be marked failed in campaign status.
- Inspect a campaign with `/broadcast_status CAMPAIGN_ID`; inspect outbox delivery failures with `/outbox_status`.

## Important configuration

- `TELEGRAM_STARS_ENABLED=true` to enable Stars checkout.
- `REFERRAL_BONUS_STARS=10` sets the invited user's internal discount credit.
- `ADMIN_IDS` is a comma-separated list of numeric Telegram user IDs.
- `ADMIN_CHAT_ID` is optional and can be a private admin group ID.
- `PHONE_ENCRYPTION_KEY` is required to use encrypted individual inventory.
- Never upload `.env` to GitHub. Do not paste bot tokens or encryption keys into chats.

## Migration note for existing Render deployments

The new Alembic head is `0004_stars_only`. On an existing database it removes the obsolete card-proof table and card-review columns from `payments`; take a PostgreSQL backup before deploying schema changes. The app uses a conservative async SQL connection pool (3 connections plus 2 overflow per process) for the Web Service and Worker. Do not delete the existing database or reset the Alembic version table.

## Tests and limits

Run:

```bash
pytest -q
python -m compileall -q app
```

The isolated working environment currently verifies the available unit tests and Python syntax. A skipped integration test requires `aiosqlite` to be installed. Actual Telegram Stars transactions, real admin delivery, Render deployment, PostgreSQL/Redis networking and UptimeRobot checks must be verified after deployment with the user's credentials and services.
