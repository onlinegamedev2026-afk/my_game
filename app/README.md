# Luck Game v7 — DigitalOcean Deployment Guide

This version is configured for deployment on a **DigitalOcean Droplet** where:

- **Host Nginx** runs directly on the Droplet — handles TLS, rate limiting, and public ports 80/443
- **Containerised Nginx** runs inside Docker — load-balances between the 2 web replicas via Docker's internal DNS
- **PostgreSQL** is a separate DigitalOcean Managed Database
- Docker Compose runs all application services (web × 2, nginx, Redis, PgBouncer, Celery, game_scheduler)
- Web replicas have **no host port binding** — only the containerised Nginx can reach them

---

## Architecture Overview

```
Internet
    |
    | :80 / :443
    |
[Host Nginx]        <-- installed on Droplet; TLS + rate limiting here
    |
    | proxy_pass → 127.0.0.1:8080
    |
[Docker: nginx]     <-- containerised; load-balances via Docker DNS
    |         |
    |  web:8000  web:8000   (no host ports — Docker internal only)
    |
[Docker: web replica-1]   [Docker: web replica-2]
         |                          |
         +------------+-------------+
                      |
              [pgbouncer]  [redis]   (Docker backend network)
                      |
      [DigitalOcean Managed PostgreSQL]  <-- external, TLS required
```

---

## Part 1 — DigitalOcean Infrastructure Setup

### 1.1 Create a Droplet

1. Go to DigitalOcean → Create → Droplets
2. Choose **Ubuntu 22.04 LTS**
3. Plan: **2 vCPU / 8 GB RAM** ($48/mo) — required for 2000 concurrent users
4. Region: choose same region as your managed DB (e.g., BLR1 / Bangalore)
5. Add your SSH key
6. Enable **VPC Network** (recommended — keeps DB traffic private)

### 1.2 Create a Managed PostgreSQL Database

1. Go to DigitalOcean → Databases → Create Database
2. Choose **PostgreSQL 16**
3. Same region as your Droplet
4. Plan: 1 GB RAM / 1 vCPU is fine to start
5. After creation, go to the database dashboard:
   - **Settings → Trusted Sources**: add your Droplet's IP address (restrict access)
   - Copy the **Private hostname** (format: `private-db-postgresql-blr1-xxxxx-do-user-xxxxxxx-0.b.db.ondigitalocean.com`)
   - Copy the **Port** (usually `25060`)
   - Copy the **Database name**, **Username**, **Password**

### 1.3 Configure Firewall (Droplet)

In DigitalOcean → Networking → Firewalls, create a firewall and allow:

| Type  | Protocol | Port  | Source       |
|-------|----------|-------|--------------|
| Inbound | TCP   | 22    | Your IP only |
| Inbound | TCP   | 80    | All          |
| Inbound | TCP   | 443   | All (for HTTPS later) |
| Outbound | All  | All   | All          |

Attach this firewall to your Droplet.

---

## Part 2 — Server Setup (on the Droplet)

SSH into your Droplet:

```bash
ssh root@YOUR_DROPLET_IP
```

### 2.1 System Update

```bash
apt update && apt upgrade -y
```

### 2.2 Install Docker

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker
```

Verify:

```bash
docker --version
docker compose version
```

### 2.3 Install Nginx on the Host

```bash
apt install -y nginx
systemctl enable nginx
systemctl start nginx
```

### 2.4 Install Git

```bash
apt install -y git
```

---

## Part 3 — Deploy the Application

### 3.1 Clone the Repository

```bash
cd /opt
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git my_game
cd my_game/app
```

> Replace the git URL with your actual repository URL.

### 3.2 Fill in the .env File

The `.env` file already exists with placeholder values. Edit it:

```bash
nano .env
```

**Values you MUST change (every `change-me` field):**

| Variable | What to set |
|---|---|
| `SECRET_KEY` | Run: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `CSRF_SECRET` | Run the same command again for a different value |
| `COOKIE_SECURE` | `false` during initial HTTP smoke test, then `true` after HTTPS |
| `DB_HOST` | Private hostname from DigitalOcean managed DB dashboard |
| `DB_PORT` | `25060` (DigitalOcean default) |
| `DB_NAME` | Database name from DO dashboard |
| `DB_USER` | Database username from DO dashboard |
| `DB_PASSWORD` | Database password from DO dashboard |
| `ADMIN_USERNAME` | Your admin login username |
| `ADMIN_PASSWORD` | Strong password (mix of letters, numbers, symbols) |
| `ADMIN_EMAIL_ID` | Your admin email address |
| `SMTP_USERNAME` | Your Gmail address (if using Gmail for OTP emails) |
| `SMTP_PASSWORD` | Gmail App Password (not your Gmail login password) |
| `SMTP_FROM_EMAIL` | Same as SMTP_USERNAME |

**Generate secrets:**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Run this twice — once for `SECRET_KEY`, once for `CSRF_SECRET`.

**Gmail App Password:** Go to Google Account → Security → 2-Step Verification → App Passwords → create one for "Mail".

### 3.3 Build and Start Docker Containers

```bash
docker compose up --build -d
```

This starts: `web` (2 replicas — no host ports), `nginx` (containerised, on `127.0.0.1:8080`), `pgbouncer`, `redis`, `celery_worker`, `celery_beat`, `celery_cleanup_worker`, `game_scheduler`.

Check all containers are running:

```bash
docker compose ps
```

All services should show `Up` or `healthy`. If any show `Exit`, check logs:

```bash
docker compose logs --tail=50 pgbouncer
docker compose logs --tail=50 web
docker compose logs --tail=50 nginx
```

**Common startup issue:** pgbouncer cannot reach the managed DB.
- Verify `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` in `.env`
- Verify your Droplet IP is in the managed DB trusted sources

Test the containerised Nginx is reachable from the Droplet (this goes through the load balancer to a web replica):

```bash
curl http://127.0.0.1:8080/health
```

Expected response: `{"status": "ok"}` or similar.

---

## Part 4 — Configure Host Nginx (Reverse Proxy)

The containerised Nginx listens on `127.0.0.1:8080` on the host. Host Nginx proxies public traffic (ports 80/443) to it and handles TLS and rate limiting.

### 4.1 Create Nginx Config

The reference config is already in the repo at `nginx/nginx.conf`. Copy it directly and then edit `server_name`:

```bash
cp nginx/nginx.conf /etc/nginx/sites-available/my_game
nano /etc/nginx/sites-available/my_game   # change server_name
```

Or paste manually:

```bash
nano /etc/nginx/sites-available/my_game
```

Replace `YOUR_DOMAIN_OR_IP` with your domain name or droplet public IP, then paste:

```nginx
# Proxy to the containerised Nginx which load-balances the 2 web replicas
upstream web_backend {
    server 127.0.0.1:8080;
}

# Rate limiting zones
limit_req_zone $binary_remote_addr zone=login_rl:10m  rate=5r/m;
limit_req_zone $binary_remote_addr zone=otp_rl:10m    rate=3r/m;
limit_req_zone $binary_remote_addr zone=bet_rl:10m    rate=30r/m;
limit_req_zone $binary_remote_addr zone=global_rl:10m rate=60r/s;

server {
    listen 80;
    server_name YOUR_DOMAIN_OR_IP;

    client_max_body_size 1m;

    # Security headers
    add_header X-Frame-Options        "SAMEORIGIN"    always;
    add_header X-Content-Type-Options "nosniff"       always;
    add_header X-XSS-Protection       "1; mode=block" always;
    add_header Referrer-Policy        "strict-origin" always;

    # Global rate limit
    limit_req zone=global_rl burst=100 nodelay;

    # Health check — no rate limiting, no logging
    location = /health {
        proxy_pass http://web_backend/health;
        proxy_set_header Host $host;
        access_log off;
    }

    # Static files
    location /static/ {
        proxy_pass       http://web_backend/static/;
        proxy_set_header Host $host;
        expires          7d;
        add_header       Cache-Control "public, immutable";
        add_header       X-Frame-Options        "SAMEORIGIN"    always;
        add_header       X-Content-Type-Options "nosniff"       always;
        add_header       X-XSS-Protection       "1; mode=block" always;
        add_header       Referrer-Policy        "strict-origin" always;
    }

    # Login — tight rate limit
    location = /login {
        limit_req zone=login_rl burst=3 nodelay;
        proxy_pass       http://web_backend/login;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # OTP — tightest rate limit
    location = /login/otp {
        limit_req zone=otp_rl burst=2 nodelay;
        proxy_pass       http://web_backend/login/otp;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Betting endpoints
    location ~ ^/games/[^/]+/bet$ {
        limit_req zone=bet_rl burst=10 nodelay;
        proxy_pass       http://web_backend;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    }

    # WebSocket
    location ~ ^/ws/ {
        proxy_pass         http://web_backend;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # Everything else
    location / {
        proxy_pass       http://web_backend;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

### 4.2 Enable the Site

```bash
ln -s /etc/nginx/sites-available/my_game /etc/nginx/sites-enabled/my_game
rm /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
```

### 4.3 Smoke Test (HTTP)

Open in your browser:

```
http://YOUR_DROPLET_IP/health
http://YOUR_DROPLET_IP/
```

The login page should appear. If it does, the stack is working.

> At this stage `COOKIE_SECURE=false` is required in `.env` because there is no HTTPS yet.
> After you add HTTPS (Part 5), change it back to `true` and restart the containers.

---

## Part 5 — Add HTTPS with Let's Encrypt (Do this after smoke test passes)

You need a **domain name** pointing to your Droplet IP before this step.

### 5.1 Point Your Domain

In your domain registrar / DNS panel, add an A record:

```
Type: A
Name: @ (or your subdomain, e.g. game)
Value: YOUR_DROPLET_PUBLIC_IP
TTL: 300
```

Wait for DNS propagation (a few minutes to an hour).

Verify:

```bash
ping YOUR_DOMAIN
```

Should resolve to your Droplet IP.

### 5.2 Install Certbot

```bash
apt install -y certbot python3-certbot-nginx
```

### 5.3 Obtain SSL Certificate

```bash
certbot --nginx -d YOUR_DOMAIN
```

Certbot will:
- Verify domain ownership via HTTP
- Obtain a free Let's Encrypt certificate
- Automatically update your Nginx config to redirect HTTP → HTTPS

### 5.4 Enable COOKIE_SECURE

After HTTPS is confirmed working, update `.env`:

```bash
nano .env
```

Change:

```env
COOKIE_SECURE=true
```

Restart the web replicas and nginx:

```bash
docker compose up -d --build web nginx
```

### 5.5 Auto-Renew Certificate

Certbot installs a systemd timer automatically. Verify:

```bash
systemctl status certbot.timer
```

Test renewal works:

```bash
certbot renew --dry-run
```

---

## Part 6 — Ongoing Operations

### View Logs

```bash
docker compose logs --tail=100 -f web
docker compose logs --tail=100 -f nginx
docker compose logs --tail=100 -f pgbouncer
docker compose logs --tail=100 -f celery_worker
docker compose logs --tail=100 -f game_scheduler
```

### Restart Services

```bash
docker compose restart web nginx
docker compose restart game_scheduler
```

### Full Restart

```bash
docker compose down
docker compose up --build -d
```

### Pull Code Updates and Redeploy

```bash
git pull
docker compose up --build -d
```

### Check Health

```bash
curl http://127.0.0.1:8080/health   # through containerised nginx → web replica
curl http://localhost/health          # through host nginx → containerised nginx → web replica
```

---

## Part 7 — Important Notes and Gotchas

### Database Connection

- `pgbouncer` inside Docker talks to the DigitalOcean managed DB over the egress network
- All app containers (web, celery) connect to `pgbouncer:5432` internally — never directly to the managed DB
- If pgbouncer fails to start, check `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` in `.env`
- `SERVER_TLS_SSLMODE=verify-ca` is mandatory for DigitalOcean managed PostgreSQL — verifies the server certificate against `certs/ca-certificate.crt`

### Nginx Config Files

There are two Nginx configs in this repo:

| File | Purpose |
|---|---|
| `nginx/nginx.conf` | **Host Nginx** — TLS, rate limiting, proxies to :8080 |
| `nginx/docker-nginx.conf` | **Containerised Nginx** — load-balances between web replicas via Docker DNS |

The containerised config is mounted automatically by Docker Compose (no manual step needed).

For the host config:
```bash
cp nginx/nginx.conf /etc/nginx/sites-available/my_game
nano /etc/nginx/sites-available/my_game   # set server_name
nginx -t && systemctl reload nginx
```

The active host Nginx config lives at `/etc/nginx/sites-available/my_game` on the Droplet.

### COOKIE_SECURE

| Stage | Value |
|---|---|
| Initial HTTP smoke test | `false` |
| After HTTPS is configured | `true` |

Never leave `false` in production with real users.

### Game Scheduler

Only one `game_scheduler` container must run at any time. The docker-compose.yml enforces `container_name: luckv7_game_scheduler` to prevent accidental duplicate containers.

### Web Container Port Binding

The 2 web replicas have **no host port binding at all** — they are not reachable from the host or the internet directly. Only the containerised Nginx container can reach them, via Docker's internal `backend` network using the service name `web:8000`. Docker DNS automatically round-robins between both replicas.

The containerised Nginx publishes `127.0.0.1:8080` to the host so host Nginx can proxy to it. Do not add host ports to the web service.

### .env File Security

```bash
chmod 600 /opt/my_game/app/.env
```

Never commit `.env` with real credentials to git.

### First-Time Database Initialization

The app creates database tables automatically on the first startup. Watch web logs during the first launch:

```bash
docker compose logs -f web
```

---

## Checklist Before Going Live

- [ ] All `change-me` values replaced in `.env`
- [ ] `docker compose ps` shows all services healthy (web ×2, nginx, pgbouncer, redis, celery*)
- [ ] `curl http://127.0.0.1:8080/health` returns ok (containerised nginx → web replica)
- [ ] `curl http://localhost/health` returns ok (host nginx → containerised nginx → web replica)
- [ ] Host Nginx config tested with `nginx -t`
- [ ] Login page loads at `http://YOUR_IP/`
- [ ] OTP email sends correctly (check SMTP settings)
- [ ] Domain A record pointing to Droplet IP
- [ ] HTTPS certificate obtained (certbot)
- [ ] `COOKIE_SECURE=true` in `.env` after HTTPS is live
- [ ] Firewall rules applied (only ports 22, 80, 443 open)
- [ ] DigitalOcean managed DB trusted sources set to Droplet IP only
- [ ] `.env` file permissions set to `chmod 600`
