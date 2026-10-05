# Backups, restore and factory reset

## What a backup holds

A backup is one `.tar.gz` file in the data volume's `backups/` folder. It contains:

- `app.db`: a consistent snapshot of the database, taken live with SQLite's `VACUUM INTO`. Nobody has to stop playing.
- `uploads/`: your uploaded logo, if any.
- `manifest.json`: the app version, the database schema revision, the time, and a SHA-256 checksum for every file.

**Never included:** Garmin data, the Garmin login token, your `.env` and older backups.

**Integration secrets** (Workers AI token, Discord webhooks, SMTP password) are inside the database. They're encrypted with your `APP_SECRET_KEY`, so they only work again with the same key. Keep a copy of that key somewhere safe, apart from the backups.

## Automatic and manual backups

- **Every night at 03:30** the worker makes a backup. It keeps the newest **7** automatic ones (set `BACKUP_RETENTION` to change that). Manual backups are never deleted automatically.
- **Admin → System → Back up now**: shown while Settings → **Backups** is on.
- **From the command line:**

  ```bash
  docker compose exec worker wp backup create --label before-upgrade
  docker compose exec worker wp backup list
  docker compose exec worker wp backup verify --latest
  ```

## Copy backups off the machine

Backups on the same disk don't help when that disk dies. Copy them somewhere else every day. For example, a root crontab entry on the host:

```cron
# 04:15 daily: mirror WeightPicks backups to another machine (key-based SSH)
15 4 * * * rsync -a --delete-after /var/lib/docker/volumes/weightpicks_wp_data/_data/backups/ backup-user@nas.local:/backups/weightpicks/
```

Or with a systemd timer: a `oneshot` service running the same `rsync`, plus a timer with `OnCalendar=*-*-* 04:15`.

Check the copies now and then: `wp backup verify NAME` works on any copied file once you put it back in the `backups/` folder.

## Restore

A restore never swaps the database under a running app. Instead:

1. **Stage it.** Pick a backup in Admin → System and choose **Restore this backup**, or upload a backup file there. Confirm with your password. From the CLI: `docker compose exec worker wp maintenance restore NAME`, then `docker compose restart`.
2. **Checks first.** The file's checksums and contents are verified. A backup from a *newer* version than the one you're running is refused, so upgrade first.
3. **The app restarts.** The worker finishes its tick and stops. On start, the web container moves the current database aside as `backups/pre-restore-<time>.db` (your uploads go alongside it), puts the backup in place, and runs any pending migrations.
4. **Log in again.** Everything is as it was when the backup was made.

If the backup came from an instance with a different `APP_SECRET_KEY`, Admin → System says the secrets can't be read. Clear them there and enter the integrations again.

**Restore drill:** do one occasionally on a spare machine. Copy a backup there, restore it, and check that the leaderboard matches. That's the only way to know your backups work.

## Factory reset

Admin → System → **Factory reset** starts over:
- **Confirm:** type the instance name and your password.
- **What it does:** the database is archived as `backups/pre-reset-<time>.db` and a fresh one is created. The web logs show a new setup token: `docker compose logs web | grep SETUP`.
- **Garmin:** data and the login token are kept, unless you tick **Also delete Garmin data**.

From the CLI: `docker compose exec worker wp maintenance reset --confirm "<instance name>"`, then `docker compose restart`.

**Changed your mind before the restart?** Run `docker compose exec worker wp maintenance cancel`. Once the app has restarted and applied it, the archived file in `backups/` is how to get back.
