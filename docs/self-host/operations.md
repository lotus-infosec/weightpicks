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

- `.env` holds install-time settings only (image version, bind address, public URL, key). After editing it: `sudo docker compose up -d`. The time zone and the public URL can be changed in Admin → Settings instead (see below).
- **`APP_SECRET_KEY`** encrypts the integration secrets in the database. Never change it on a running instance: the secrets become unreadable (Admin → System offers to clear them so you can enter them again).
- Integration secrets (Discord webhooks, Workers AI token, SMTP password) are entered in Admin → Settings and stored encrypted.

## Time zone

The time zone decides which day a weigh-in counts for, the 04:00–11:00 weigh-in window, and when markets drop and lock. It is chosen on `/setup` and stored in the database. `WP_TIMEZONE` in `.env` only pre-fills that step, so editing `.env` afterwards changes nothing.

Change it in **Admin → Settings → Instance → Time zone**. **Review change** shows what happens before anything does:

- Markets whose results are already in settle normally.
- Every other open market and event is **pushed and refunded** (it was priced on the old zone's days). A parlay loses those legs and is checked on the rest. Players get their refunds in Discord, plus one "time zone changed" post.
- Today's daily markets are posted again in the new zone if today's drop time has passed and the lock hasn't. Weekly and monthly markets return at their next scheduled drop.
- No restart is needed. Weigh-ins and workouts already synced are placed on the right day in the new zone automatically.

From the command line (it shows the same summary and needs `--yes` to refund):

```bash
sudo docker compose exec worker wp settings timezone America/Chicago --yes   # names: timedatectl list-timezones
```

## Public URL

Links in Discord posts and password-reset emails start with the public URL. The installer writes it to `.env` as `WP_BASE_URL` from `--hostname`. You can set or change it in **Admin → Settings → Instance → Public URL** (no restart), which then takes precedence; tick *Use the .env value* to go back. It must be `https://` with no path, for example `https://picks.example.com`.

If links still point to `127.0.0.1`, the Admin dashboard and System page show a warning. From the command line: `sudo docker compose exec worker wp settings public-url https://picks.example.com`.

## Uninstall

```bash
cd /opt/weightpicks
sudo docker compose down               # keeps the data volumes
sudo docker volume rm weightpicks_wp_data weightpicks_wp_garmin weightpicks_wp_garmin_tokens   # deletes everything
sudo cloudflared service uninstall
sudo rm -rf /opt/weightpicks
```

Then delete the tunnel in the Cloudflare dashboard.
