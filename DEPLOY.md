# Box2Fit Platform — Hosting & Deployment Guide

Instructions for deploying the Box2Fit membership platform at
**health.box2fit.com**. Written for the person operating the server — no
prior knowledge of this project is assumed.

The application is a standard Python/Flask web app served by gunicorn behind
nginx. It needs no Node, no PHP, no database server (v1 uses SQLite; see
"Scaling up" at the end).

---

## 1. Server requirements

- Linux (Ubuntu 22.04/24.04 recommended), root or sudo access
- **Python 3.12** (`python3 --version`) with `venv`
- **nginx** and **certbot** (`python3-certbot-nginx`) for HTTPS
- 1 GB RAM recommended (runs in 512 MB + swap)
- Outbound HTTPS open to: api.stripe.com, api.sendgrid.com, api.twilio.com,
  graph.facebook.com, www.google-analytics.com
- `cron` available

## 2. Get the code (read-only deploy key)

The code lives in a private GitHub repository. To get access:

1. On the server, create a key (skips itself if one exists) and print it:
   ```bash
   [ -f ~/.ssh/id_ed25519.pub ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -q; cat ~/.ssh/id_ed25519.pub
   ```
2. Send the printed `ssh-ed25519 ...` line to the platform maintainer — they
   will add it as a read-only deploy key.
3. Once confirmed, clone (adjust the base path to your convention):
   ```bash
   sudo mkdir -p /var/www && cd /var/www
   git clone git@github.com:jevansxje-gif/box2fit-platform.git
   cd box2fit-platform
   ```

## 3. Install

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
```

## 4. Configure

Create `/var/www/box2fit-platform/.env` (never commit this file):

```bash
cat > .env <<EOF
SECRET_KEY=$(openssl rand -hex 32)
JWT_SECRET=$(openssl rand -hex 32)
SITE_BASE_URL=https://health.box2fit.com
CELERY_TASK_ALWAYS_EAGER=1
EOF
chmod 600 .env
```

Additional keys (Stripe/SendGrid/Twilio/Meta/GA4) are added to this same
file later by the platform maintainer — see `.env.example` in the repo for
the full list. The app runs fine without them (payment step shows a
placeholder until Stripe keys exist).

## 5. Database + seed data

```bash
FLASK_APP=wsgi.py .venv/bin/flask db upgrade
.venv/bin/python -m scripts.seed
```

Expected: migration lines, then `seed complete`. This creates the schema,
the class schedule, and two staff logins (the maintainer will rotate the
passwords after handover).

**If migrating from a previous server:** instead of running the seed, stop
the app on the old server and copy its `platform_dev.db` file into this
directory after `flask db upgrade` — that carries over all members,
bookings and payments. Coordinate with the maintainer.

## 6. Run as a service

```bash
cat > /etc/systemd/system/box2fit.service <<'EOF'
[Unit]
Description=Box2Fit Platform (gunicorn)
After=network.target

[Service]
WorkingDirectory=/var/www/box2fit-platform
ExecStart=/var/www/box2fit-platform/.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8090 --timeout 60 wsgi:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now box2fit
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8090/healthz   # expect: 200
```

Note gunicorn binds **127.0.0.1 only** — nginx is the public face.

## 7. Background jobs (cron)

The app relies on a jobs runner every 10 minutes (class reminders, no-show
marking, waitlist processing, schedule generation):

```bash
( crontab -l 2>/dev/null | grep -v run_jobs; echo '*/10 * * * * cd /var/www/box2fit-platform && .venv/bin/python -m scripts.run_jobs >> /var/log/box2fit-jobs.log 2>&1' ) | crontab -
```

## 8. DNS, nginx, HTTPS

1. **DNS**: add `health` as an A record on box2fit.com pointing at this
   server's IP (done wherever box2fit.com's DNS is managed — the main
   Shopify site is unaffected; this only creates the subdomain).
2. **nginx**:
   ```bash
   cat > /etc/nginx/sites-available/box2fit <<'EOF'
   server {
       server_name health.box2fit.com;
       listen 80;
       client_max_body_size 5m;
       location / {
           proxy_pass http://127.0.0.1:8090;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   EOF
   ln -sf /etc/nginx/sites-available/box2fit /etc/nginx/sites-enabled/box2fit
   nginx -t && systemctl reload nginx
   ```
3. **HTTPS** (free, auto-renewing, once DNS resolves):
   ```bash
   certbot --nginx -d health.box2fit.com --redirect --agree-tos -m <admin-email> --no-eff-email
   ```

## 9. Verify

- https://health.box2fit.com/healthz → `{"status":"ok"}`
- https://health.box2fit.com/ → homepage; /youth → booking funnel
- https://health.box2fit.com/ops/login → staff login page
- `systemctl status box2fit` → active (running)
- After 10+ minutes: `/var/log/box2fit-jobs.log` has a line per run

## 10. Updates (the standing routine)

When the maintainer announces an update:

```bash
cd /var/www/box2fit-platform && git pull && .venv/bin/pip install -r requirements.txt -q && FLASK_APP=wsgi.py .venv/bin/flask db upgrade && systemctl restart box2fit && echo "DEPLOY OK"
```

## 11. Backups (please enable)

The entire application state is one file: `platform_dev.db` (plus `.env`).
A nightly copy is sufficient:

```bash
( crontab -l 2>/dev/null | grep -v db-backup; echo '30 3 * * * cp /var/www/box2fit-platform/platform_dev.db /var/backups/box2fit-$(date +\%u).db # db-backup' ) | crontab -
```

(keeps 7 rotating daily copies in /var/backups)

## Scaling up (later, coordinated with the maintainer)

The app is MariaDB/MySQL-ready (`DATABASE_URL` env var) and Celery/Redis-
ready (real-time background workers instead of cron) — both are planned for
when membership volume justifies it. Nothing about this initial setup blocks
that move.

## Contact

Platform maintainer: Joey (kevans3221@gmail.com) — for deploy keys, update
announcements, Stripe/SendGrid credentials, and anything unclear here.
