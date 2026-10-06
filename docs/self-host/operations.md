# Operations

Run these from the install folder (`cd /opt/weightpicks`). `docker compose` reads `.env` there, so use `sudo`.

## Status and logs

```bash
sudo docker compose ps                         # both "healthy"
curl -s http://127.0.0.1:8000/healthz          # {"status":"ok"}
sudo docker compose logs -f --tail 100 worker  # sync, drops, settlement, Discord
sudo docker compose logs -f --tail 100 web     # one JSON line per request (route, status, ms)
```

Logs are JSON; `| jq` makes them readable. They never contain passwords, tokens, webhook URLs or emails. Docker keeps 5 × 10 MB per container.

Admin → **System** shows the same health in the browser: last sync and data freshness, last drop and settlement, outbox backlog, AI usage, ledger check, heartbeats, versions and backups.

## Restart, stop, start

```bash
sudo docker compose restart            # both containers
sudo docker compose down               # stop (data stays in the volumes)
sudo docker compose up -d              # start
```

Containers restart on their own after a crash or a reboot (`restart: unless-stopped`), as long as Docker starts at boot (`sudo systemctl enable docker`). **Never** run `docker compose down -v`: `-v` deletes the database and the Garmin token.

## Backups

Every night at 03:30 the worker makes a backup and keeps the newest 7. Make one by hand before anything risky:

```bash
sudo docker compose exec worker wp backup create --label before-something
sudo docker compose exec worker wp backup list
sudo docker compose exec worker wp backup verify --latest
```

Copy them off the machine every day, and keep `APP_SECRET_KEY` (from `.env`) somewhere safe and separate. Restore, off-box copies and the restore drill: [backups.md](../backups.md).

## Players and the game

Everything else is in Admin: players (freeze, ban, reset password), the bank (bailouts, adjustments, economy), markets (void), props, special events, seasons, settings and the audit log. Seasons and Goal Reached: [seasons.md](../seasons.md).

## Factory reset

Starts over with an empty instance (the database is archived in `backups/` first): Admin → System → **Factory reset**, or

```bash
sudo docker compose exec worker wp maintenance reset --confirm "<instance name>"
sudo docker compose restart
sudo docker compose logs web | grep 'SETUP TOKEN'
```

Garmin data and the login token are kept unless you tick **Also delete Garmin data**.

## Secrets and settings

- `.env` holds install-time settings only (image version, bind address, public URL, key). After editing it: `sudo docker compose up -d`.
- **`APP_SECRET_KEY`** encrypts the integration secrets in the database. Never change it on a running instance: the secrets become unreadable (Admin → System offers to clear them so you can enter them again).
- Integration secrets (Discord webhooks, Workers AI token, SMTP password) are entered in Admin → Settings and stored encrypted.

## Uninstall

```bash
cd /opt/weightpicks
sudo docker compose down               # keeps the data volumes
sudo docker volume rm weightpicks_wp_data weightpicks_wp_garmin weightpicks_wp_garmin_tokens   # deletes everything
sudo cloudflared service uninstall
sudo rm -rf /opt/weightpicks
```

Then delete the tunnel in the Cloudflare dashboard.
