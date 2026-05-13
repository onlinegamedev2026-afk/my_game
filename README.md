# Luck Game

A production-ready, multi-player **card game betting platform** built with FastAPI, PostgreSQL, Redis, and WebSockets. Supports three live card games with real-time state sync, a three-tier account hierarchy, a role-based wallet system, and automated game orchestration — all containerised with Docker Compose and deployable behind a host-based Nginx reverse proxy.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Games](#games)
- [Account Hierarchy & Roles](#account-hierarchy--roles)
- [Authentication Flow](#authentication-flow)
- [WebSocket Protocol](#websocket-protocol)
- [Database Schema](#database-schema)
- [API Reference](#api-reference)
- [Environment Variables](#environment-variables)
- [Local Development](#local-development)
- [Production Deployment](#production-deployment)
- [Celery Tasks](#celery-tasks)
- [Game Scheduler](#game-scheduler)
- [Security Design](#security-design)

---

## Features

| Area | Details |
|---|---|
| **Live Card Games** | Teen Patti, Andar Bahar, Color Guessing — all with animated, real-time card dealing |
| **Real-time Sync** | WebSocket + Redis Pub/Sub broadcasts game phase, timers, bets, and winners to every connected client instantly |
| **Betting Engine** | Players bet on Side A or Side B during a configurable betting window; payouts settle with idempotency keys and `SELECT FOR UPDATE` wallet locks |
| **Bias Engine** | Game outcomes are weighted toward balancing payout exposure — if Side A holds more money, the engine favours Side B |
| **Role-based Hierarchy** | Three-tier tree: ADMIN → AGENT → USER; every actor can only manage their direct children |
| **Wallet Ledger** | All money movements (credits, debits, adjustments, payouts) are permanently recorded before any balance update |
| **Two-factor Login** | ADMIN and AGENT login via password + email OTP; USER login via password + math captcha |
| **Single Active Session** | Redis enforces one live session per account; new login from any device invalidates the previous one |
| **Automated Cleanup** | Celery Beat fires a daily job at 02:00 UTC to archive old records and auto-delete long-inactive accounts |
| **CSV Exports** | Admins and agents can export their transaction history and child account list as CSV |
| **Horizontal Scaling** | Web tier runs as 2 Docker Compose replicas; Nginx upstream round-robins across them |
| **Connection Pooling** | PgBouncer (transaction mode) sits between all app containers and the database |

---

## Architecture

```
                        ┌─────────────────────────────────────┐
                        │           Internet (public)          │
                        └─────────────────┬───────────────────┘
                                          │ :80 / :443
                                          ▼
                        ┌─────────────────────────────────────┐
                        │    Host Nginx (reverse proxy)        │
                        │  upstream web_backend {              │
                        │    127.0.0.1:8000;  (replica 1)     │
                        │    127.0.0.1:8001;  (replica 2)     │
                        │  }                                   │
                        └────────────┬────────────────────────┘
                                     │ HTTP / WS
                     ┌───────────────┴────────────────┐
                     ▼                                ▼
        ┌────────────────────┐          ┌────────────────────┐
        │  web replica-1     │          │  web replica-2     │
        │  FastAPI + Gunicorn│          │  FastAPI + Gunicorn│
        │  :8000             │          │  :8001             │
        └─────────┬──────────┘          └──────────┬─────────┘
                  │                                 │
                  └──────────────┬──────────────────┘
                                 │ Docker internal network (backend)
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                   ▼
   ┌─────────────────┐  ┌──────────────┐  ┌──────────────────────┐
   │    PgBouncer    │  │    Redis     │  │   game_scheduler     │
   │  (txn pooling)  │  │  7-alpine    │  │  (distributed lock,  │
   │  :5432          │  │  :6379       │  │   single instance)   │
   └────────┬────────┘  └──────┬───────┘  └──────────┬───────────┘
            │                  │                       │
            │            Pub/Sub (luck_game_events)    │ publishes
            │                  │◄──────────────────────┘
            ▼                  │
   ┌─────────────────┐         │ Celery broker / result backend
   │  DigitalOcean   │  ┌──────┴────────────────────────────────┐
   │  Managed PG     │  │  celery_worker  celery_cleanup_worker  │
   │  (external TLS) │  │  celery_beat                          │
   └─────────────────┘  └───────────────────────────────────────┘
```

**Network isolation:**

- `backend` — internal Docker network; containers cannot reach the internet
- `egress` — allows pgbouncer + celery workers to reach the external managed database

---

## Tech Stack

| Layer | Technology | Version |
|---|---|---|
| Web framework | FastAPI | 0.115.6 |
| ASGI server | Gunicorn + Uvicorn workers | 23.0.0 / 0.34.0 |
| Templating | Jinja2 | 3.1.6 |
| Database | PostgreSQL (psycopg v3, async pool) | psycopg 3.2.13 |
| Connection pool | PgBouncer (transaction mode) | latest |
| Cache / Pub-Sub | Redis | 7-alpine |
| Async tasks | Celery | 5.4.0 |
| Password hashing | passlib[bcrypt] | 1.7.4 |
| Container runtime | Docker Compose v2 | — |
| Reverse proxy | Nginx (host-installed) | — |
| Frontend | Vanilla JS + Jinja2 HTML | — |

---

## Project Structure

```
using_claude_v7/
│
├── main.py                     # All HTTP routes + WebSocket endpoint (877 lines)
│
├── core/
│   ├── config.py               # Settings loaded from .env (117 lines)
│   ├── database.py             # Schema DDL + async psycopg pool (246 lines)
│   ├── redis_client.py         # Async + sync Redis client helpers
│   ├── security.py             # HMAC session signing, CSRF, bcrypt
│   └── logging_config.py       # Structured logging setup
│
├── services/
│   ├── auth_service.py         # Credential verification, actor lookups
│   ├── game_orchestrator.py    # Game state (Redis), bet placement, settlement
│   ├── hierarchy_service.py    # Account CRUD within the ADMIN→AGENT→USER tree
│   ├── wallet_service.py       # Balance transfers with SELECT FOR UPDATE locks
│   ├── session_service.py      # Redis-backed single active session per user
│   ├── otp_service.py          # 6-digit OTP generation and verification (Redis TTL)
│   └── captcha_service.py      # Math captcha generation and verification
│
├── games/
│   ├── tin_patti.py            # Teen Patti deck + hand evaluation logic
│   ├── andar_bahar.py          # Andar Bahar joker + deal logic
│   └── color_guessing.py       # Red / Blue random outcome
│
├── scheduler/
│   └── game_scheduler.py       # Standalone game-cycle orchestrator (400+ lines)
│
├── realtime/
│   └── manager.py              # WebSocket manager + Redis Pub/Sub listener
│
├── tasks/
│   ├── celery_app.py           # Celery app config + Beat schedule
│   └── cleanup.py              # Archive + auto-delete Celery tasks
│
├── transactions/
│   └── ledger.py               # Wallet transfer logic (SELECT FOR UPDATE)
│
├── utils/
│   ├── money.py                # Decimal(18,3) quantisation helpers
│   └── identity.py             # Account ID + random password generation
│
├── models/
│   └── schemas.py              # Pydantic / dataclass schemas (Actor)
│
├── templates/                  # Jinja2 HTML templates
│   ├── base.html
│   ├── login.html
│   ├── otp.html
│   ├── dashboard.html
│   ├── games.html
│   ├── tin_patti.html
│   ├── andar_bahar.html
│   └── color_guessing.html
│
├── static/
│   ├── styles.css
│   ├── common.js               # Shared utilities (WebSocket, CSRF, helpers)
│   └── games/
│       ├── game_core.js        # Unified game state machine (phase management, timers, bets)
│       ├── tin_patti.js
│       ├── andar_bahar.js
│       └── color_guessing.js
│
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env                        # Local secrets (never committed)
└── README.md                   # Deployment guide
```

---

## Games

### Teen Patti (`/games/tin-patti`)

Standard 52-card deck. Three cards dealt to each of two hands (Side A and Side B). Winner determined by standard Teen Patti hand rankings (Trail > Pure Sequence > Sequence > Color > Pair > High Card).

### Andar Bahar (`/games/andar-bahar`)

A joker card is drawn first and revealed to players. Cards are then dealt alternately to Andar (Side A) and Bahar (Side B) until a card matching the joker's rank appears. The side that receives the matching card wins.

### Color Guessing (`/games/color-guessing`)

Simplest game. Players bet on Red (Side A) or Blue (Side B). The game scheduler draws a random outcome weighted by the bias engine.

### Bias Engine

All three games apply a house bias: if the total money on Side A exceeds Side B, the engine adds weight toward Side B winning — and vice versa. This reduces payout volatility while keeping outcomes non-deterministic.

### Game Cycle Timing

Controlled via `.env`:

| Variable | Default | Description |
|---|---|---|
| `BETTING_WINDOW_SECONDS` | 40 | Time players can place bets |
| `GAME_INITIATION_SECONDS` | 10 | Transition delay before running |
| `CARD_DRAWING_DELAY_SECONDS` | 3 | Pause between each card reveal |
| `AFTER_GAME_COOLDOWN_SECONDS` | 10 | Cooldown before next cycle begins |

---

## Account Hierarchy & Roles

```
ADMIN  (one, created at startup)
  └── AGENT  (created by ADMIN)
        └── USER  (created by AGENT)
```

- Every actor can only see and manage their **direct children**.
- Money flows strictly **down** (parent credits child) and **up** (parent debits child).
- An ADMIN can adjust their own wallet balance directly (add or deduct).
- Deactivating an account blocks new actions but allows in-flight game rounds to complete.
- Deleting an account recursively deletes the full subtree and zero-balances all wallets via ledger entries.

---

## Authentication Flow

```
Browser                        FastAPI                        Redis / DB
   │                              │                               │
   │── GET / ──────────────────► │ Serve login page + captcha    │
   │                              │──── store captcha ──────────► │
   │── POST /login ─────────────► │                               │
   │   {username, password,       │◄─── verify captcha ──────────│
   │    role, captcha_answer}     │◄─── verify bcrypt ───────────│
   │                              │                               │
   │          ┌───────────────────┤                               │
   │          │ USER role?        │                               │
   │          │  → set session,   │──── store session nonce ────►│
   │          │    redirect /games│                               │
   │          │ ADMIN/AGENT?      │                               │
   │          │  → generate OTP   │──── store 6-digit OTP ──────►│
   │          │  → send email     │                               │
   │          │  → redirect /otp  │                               │
   │          └───────────────────┤                               │
   │                              │                               │
   │── POST /login/otp ─────────►│◄─── verify OTP ─────────────│
   │   {otp_code}                 │──── set session cookie ──────►│
   │◄── 302 /dashboard ──────────│                               │
```

**Session token** — a signed cookie (`luck_session`) containing `user_id`, `role`, `expiry`, and a random `nonce`. The nonce is stored in Redis; any mismatch (e.g. from a new login) invalidates the old session immediately.

**Force-login** — if a session is already active when you log in, you are shown a confirmation page. Confirming invalidates the old session and issues a new one.

---

## WebSocket Protocol

**Endpoint:** `GET /ws/games/{game_key}`  
Example: `wss://yourdomain.com/ws/games/tin-patti`

The browser upgrades to WebSocket after validating the session cookie. All game state is pushed from the server; clients never send game data over WebSocket (bets are placed via HTTP POST).

### Server → Client message shape

```json
{
  "event": "server_state",
  "data": {
    "game_key": "tin-patti",
    "session_id": "uuid",
    "phase": "BETTING",
    "remaining_seconds": 35,
    "group_a_total": "120.000",
    "group_b_total": "80.000",
    "cards_dealt": [],
    "winner": null,
    "last_10_winners": ["A", "B", "A", "B", "A"]
  }
}
```

### Phase lifecycle

```
IDLE ──► BETTING ──► INITIATING ──► RUNNING ──► SETTLING ──► IDLE
```

| Phase | What happens |
|---|---|
| `BETTING` | Betting window open; countdown timer shown |
| `INITIATING` | Betting closed; preparing game engine |
| `RUNNING` | Cards dealt one by one with `CARD_DRAWING_DELAY_SECONDS` between each |
| `SETTLING` | Wallets locked, payouts calculated and recorded, winner broadcast |
| `IDLE` | Cooldown before next cycle |

### Role-based visibility

- **USER** — sees own bet amount and result only; group totals hidden
- **ADMIN / AGENT** — sees group totals (Side A vs Side B), all bets overview

---

## Database Schema

### Core tables

**`accounts`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `username` | TEXT | Unique login name |
| `display_name` | TEXT | |
| `email` | TEXT | Required for ADMIN/AGENT (OTP target) |
| `role` | ENUM | `ADMIN`, `AGENT`, `USER`, `SYSTEM` |
| `password_hash` | TEXT | bcrypt |
| `parent_id` | UUID | FK → accounts.id (nullable for ADMIN) |
| `status` | ENUM | `ACTIVE`, `INACTIVE` |
| `created_at` | TIMESTAMPTZ | |

**`wallets`**

| Column | Type | Notes |
|---|---|---|
| `wallet_id` | UUID | |
| `owner_id` | UUID | FK → accounts.id |
| `current_balance` | NUMERIC(18,3) | All money is stored to 3 decimal places |
| `status` | ENUM | `ACTIVE`, `LOCKED`, `FROZEN`, `CLOSED` |
| `version` | INT | Optimistic lock counter |

**`wallet_transactions`** (ledger)

| Column | Type | Notes |
|---|---|---|
| `transaction_id` | UUID | |
| `idempotency_key` | TEXT | Unique; prevents double-spend |
| `transaction_type` | TEXT | `BET_DEBIT`, `BET_WIN_CREDIT`, `BET_REFUND`, `PARENT_TO_CHILD_CREDIT`, etc. |
| `from_wallet_id` / `to_wallet_id` | UUID | |
| `amount` | NUMERIC(18,3) | |
| `balance_before` / `balance_after` | NUMERIC(18,3) | Per wallet side |
| `status` | ENUM | `PENDING`, `SUCCESS`, `FAILED` |

**`game_sessions`**

| Column | Type | Notes |
|---|---|---|
| `session_id` | UUID | |
| `game_key` | TEXT | `TIN_PATTI`, `ANDAR_BAHAR`, `COLOR_GUESSING` |
| `status` | ENUM | `BETTING`, `INITIATING`, `RUNNING`, `SETTLING`, `COMPLETED`, `FAILED` |
| `group_a_total` / `group_b_total` | NUMERIC(18,3) | |
| `winner` | TEXT | `A`, `B`, `TIE`, or NULL |
| `payload` | JSONB | Full game result (cards, hand rankings, etc.) |

**`bets`**

| Column | Type | Notes |
|---|---|---|
| `bet_id` | UUID | |
| `session_id` | UUID | FK → game_sessions |
| `player_id` | UUID | FK → accounts |
| `side` | TEXT | `A` or `B` |
| `amount` | NUMERIC(18,3) | |
| `status` | ENUM | `PLACED`, `WON`, `LOST`, `REFUNDED` |

### Archive tables

After 30 days, `bets` and `game_sessions` rows are moved (not copied) to `bets_archive` and `game_sessions_archive`. Wallet transactions are **copied** to `wallet_transactions_archive` after 90 days (originals retained for audit).

> The schema is created at startup (`database.py`) — there are no migration files. First launch bootstraps all tables and the ADMIN account.

---

## API Reference

### Authentication

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | — | Login page |
| `POST` | `/login` | — | Verify credentials + captcha |
| `POST` | `/login/force` | — | Force-kick existing session |
| `POST` | `/login/otp` | — | Verify OTP, set session |
| `POST` | `/logout` | Session | Invalidate session |

### Account Management (ADMIN / AGENT)

| Method | Path | Description |
|---|---|---|
| `GET` | `/dashboard` | Paginated child account list |
| `POST` | `/children` | Create child account |
| `POST` | `/children/{id}/status` | Activate / deactivate child |
| `POST` | `/children/{id}/delete` | Delete child subtree |
| `POST` | `/children/email-otp/send` | Send OTP to verify agent email |
| `POST` | `/children/email-otp/verify` | Confirm agent email OTP |
| `GET` | `/credentials/generate` | Generate random username + password |
| `POST` | `/password/update` | Change own password |

### Wallet

| Method | Path | Description |
|---|---|---|
| `POST` | `/wallet/admin/adjust` | Admin adjusts own balance |
| `POST` | `/wallet/{child_id}/add` | Transfer from parent to child |
| `POST` | `/wallet/{child_id}/deduct` | Transfer from child to parent |
| `GET` | `/download/transactions` | CSV export of transactions |
| `GET` | `/download/children` | CSV export of child accounts |

### Games

| Method | Path | Description |
|---|---|---|
| `GET` | `/games` | Game selection page |
| `GET` | `/games/{game_key}` | Game console (e.g. `/games/tin-patti`) |
| `GET` | `/api/me` | Current actor + balance (JSON) |
| `GET` | `/api/games/{game_key}/my-bets` | Player's bets in current session |
| `POST` | `/games/{game_key}/bet` | Place a bet `{amount, side}` |
| `GET` | `/ws/games/{game_key}` | WebSocket upgrade |

### Health

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `{"status": "ok"}` |
| `GET` | `/ready` | Checks DB + Redis; returns per-component status |

---

## Environment Variables

Copy `.env.example` to `.env` and fill in every field marked `change-me`.

### App

| Variable | Default | Description |
|---|---|---|
| `APP_NAME` | `Luck Game` | Display name across templates |
| `APP_ENV` | `development` | `development` / `local-prod` / `production` |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `SECRET_KEY` | — | 256-bit hex; signs session cookies |
| `CSRF_SECRET` | — | 256-bit hex; signs CSRF tokens |
| `COOKIE_SECURE` | `false` | Set `true` after HTTPS is live |
| `SESSION_TIMEOUT_HOUR` | `8` | Session TTL in hours (`-1` = no expiry) |

### Database

| Variable | Default | Description |
|---|---|---|
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | `luck_game_v2` | Database name |
| `DB_USER` | `dev_user` | |
| `DB_PASSWORD` | `dev_pass` | |
| `SERVER_TLS_SSLMODE` | `require` | `require` for DigitalOcean managed DB; `disable` for local |
| `DB_POOL_MIN` | `2` | Min psycopg pool connections per container |
| `DB_POOL_MAX` | `10` | Max psycopg pool connections per container |
| `PGBOUNCER_MAX_CLIENT_CONN` | `500` | PgBouncer client cap |
| `PGBOUNCER_DEFAULT_POOL_SIZE` | `30` | PgBouncer pool size |

### Redis & Celery

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Session cache |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Celery message broker |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/2` | Celery result storage |
| `REDIS_PUBSUB_CHANNEL` | `luck_game_events` | Channel for real-time broadcasts |

### Admin Seed

| Variable | Default | Description |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Created at first startup |
| `ADMIN_PASSWORD` | `admin123` | **Change this** |
| `ADMIN_EMAIL_ID` | `admin@example.com` | Used for OTP login |

### SMTP (for OTP emails)

| Variable | Description |
|---|---|
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USERNAME` | Gmail address |
| `SMTP_PASSWORD` | Gmail App Password |
| `SMTP_FROM_EMAIL` | Sender address |
| `SMTP_USE_TLS` | `true` |
| `SMTP_DELETE_SENT_COPY` | `true` — auto-deletes sent mail from Gmail via IMAP |
| `SMTP_IMAP_HOST` | `imap.gmail.com` |
| `SMTP_IMAP_PORT` | `993` |
| `SMTP_SENT_MAILBOX` | `[Gmail]/Sent Mail` |

### Game Timing

| Variable | Default | Description |
|---|---|---|
| `BETTING_WINDOW_SECONDS` | `40` | Betting phase duration |
| `GAME_INITIATION_SECONDS` | `10` | Transition before running |
| `AFTER_GAME_COOLDOWN_SECONDS` | `10` | Cooldown after settling |
| `CARD_DRAWING_DELAY_SECONDS` | `3` | Delay between card reveals (float) |
| `GAME_SCHEDULER_ENABLED` | `false` | Only `true` in the scheduler container |

---

## Local Development

### Prerequisites

- Docker Desktop (with Compose v2)
- Python 3.11+
- A local PostgreSQL instance **or** use the Docker setup

### Run everything with Docker Compose

```bash
cd using_claude_v7
cp .env.example .env       # fill in your values
docker compose up --build
```

Services started: `web` (×2 replicas), `pgbouncer`, `redis`, `celery_worker`, `celery_beat`, `celery_cleanup_worker`, `game_scheduler`.

### Verify

```bash
curl http://127.0.0.1:8000/health   # replica 1
curl http://127.0.0.1:8001/health   # replica 2
```

### Useful commands

```bash
# Follow web logs
docker compose logs -f web

# Check all service health
docker compose ps

# Open a shell in the web container
docker compose exec web bash

# Re-run only the web containers after a code change
docker compose up --build -d web

# Trigger the daily cleanup manually
docker compose exec celery_worker \
  celery -A tasks.celery_app.celery_app call tasks.cleanup.daily_cleanup_job
```

### Running without Docker (for quick iteration)

```bash
cd using_claude_v7
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# Set SERVER_TLS_SSLMODE=disable in .env for a local DB
export $(cat .env | xargs)

# Start the web server
gunicorn main:app -k uvicorn.workers.UvicornWorker --workers 2 --bind 0.0.0.0:8000

# In a separate terminal — start Celery worker
celery -A tasks.celery_app.celery_app worker -Q celery --loglevel=info

# In a separate terminal — start the game scheduler
python -m scheduler.game_scheduler
```

---

## Production Deployment

See [using_claude_v7/README.md](using_claude_v7/README.md) for the complete step-by-step DigitalOcean deployment guide covering:

- Droplet provisioning and firewall rules
- Managed PostgreSQL setup (TLS, trusted sources)
- Nginx install + config (with upstream load-balancing across 2 web replicas)
- Let's Encrypt SSL via Certbot
- Environment hardening (`COOKIE_SECURE`, `chmod 600 .env`)

### Production Nginx upstream (summary)

```nginx
upstream web_backend {
    server 127.0.0.1:8000;   # web replica 1
    server 127.0.0.1:8001;   # web replica 2
}
```

All `proxy_pass` directives point to `http://web_backend`.

---

## Celery Tasks

| Task | Queue | Trigger | Description |
|---|---|---|---|
| `send_email_job` | `celery` | On-demand | Sends OTP or notification email via SMTP; optionally deletes sent copy via IMAP |
| `daily_cleanup_job` | `cleanup` | Celery Beat 02:00 UTC | Orchestrates the full archival sequence below |
| `archive_old_bets_job` | `cleanup` | Via daily | Moves WON/LOST/REFUNDED bets older than 30 days → `bets_archive` |
| `archive_old_game_sessions_job` | `cleanup` | Via daily | Moves completed sessions older than 30 days → `game_sessions_archive` |
| `copy_old_wallet_transactions_job` | `cleanup` | Via daily | Copies wallet txs older than 90 days → `wallet_transactions_archive` |
| `auto_delete_inactive_accounts_job` | `cleanup` | Via daily | Deletes AGENT/USER accounts inactive for 30+ days (subtree delete) |

**Queue design:**

- `celery` — default queue; email tasks; multiple workers can run in parallel
- `cleanup` — `concurrency=1` so archival steps run sequentially (bets archived before sessions, preventing FK violations)

---

## Game Scheduler

`scheduler/game_scheduler.py` runs as a single standalone container (`game_scheduler`). It must never be scaled beyond one instance per environment.

**Distributed lock:** For each game, the scheduler acquires a Redis lock (`luck:lock:game:{game_key}`) with a heartbeat renewed every 5 seconds. If the container crashes, the lock expires and a replacement instance can take over cleanly.

**Cycle loop (per game):**

```
1. Acquire Redis lock
2. IDLE    → open betting window, publish server_state, start countdown
3. BETTING → close betting, record group totals
4. INITIATING → run game engine (TinPattiGame.play() / AndarBaharGame.play() / etc.)
5. RUNNING  → broadcast cards one by one with CARD_DRAWING_DELAY_SECONDS delay
6. SETTLING → SELECT FOR UPDATE wallets, compute payouts, insert wallet_transactions,
              update bet statuses, write game_session result to DB
7. IDLE    → wait AFTER_GAME_COOLDOWN_SECONDS, repeat
```

All state transitions are published to the `luck_game_events` Redis Pub/Sub channel. Every web replica's `realtime/manager.py` listens on this channel and fans out to matching WebSocket connections.

---

## Security Design

| Concern | Approach |
|---|---|
| Password storage | bcrypt via passlib (PBKDF2-HMAC-SHA256, 150k iterations) |
| Session signing | HMAC-SHA256 with `SECRET_KEY`; nonce stored in Redis |
| CSRF protection | Per-form CSRF token signed with `CSRF_SECRET` |
| Session fixation | New nonce issued on every login |
| Double-login | Redis nonce mismatch immediately invalidates the old session |
| SQL injection | Parameterised queries only (psycopg v3 native) |
| Race conditions | `SELECT FOR UPDATE` on wallet + game_session rows before any money movement |
| Double-spend | Idempotency keys on all wallet transactions and bet payouts |
| Money precision | `Decimal(18,3)` throughout; no floats touch balances |
| Rate limiting | Nginx per-IP limits: 60 req/s global, 5 req/min login, 3 req/min OTP |
| Network exposure | Web containers bind only to `127.0.0.1`; `backend` network is internal-only |
| Secrets | `.env` never committed; `chmod 600` on the server |
| Cookie flags | `HttpOnly`, `SameSite=Lax`, `Secure` (enabled after HTTPS) |
