# Connect Garmin

WeightPicks reads the subject's weigh-ins, daily totals and workouts from Garmin Connect through [GarminDB](https://github.com/tcgoetz/GarminDB). The Garmin password is typed once and **never stored**: only a login token is kept, in its own Docker volume (`wp_garmin_tokens`), mounted only by the worker.

## Log in once

On the server:

```bash
cd /opt/weightpicks
sudo docker compose run --rm garmin-login
```

It asks how to sign in:

1. **Email and password**, typed in the terminal (the password is hidden). Garmin sometimes refuses scripted logins with "429" or "Cloudflare" errors. If so, don't retry in a loop: use option 2.
2. **Browser sign-in.**
   - It prints a link to Garmin's own sign-in page. Open it in a browser (any computer) and sign in, including MFA.
   - The browser ends on a page that fails to load. That's expected: copy the whole address from the address bar (it contains `ticket=ST-…`).
   - Paste it at the hidden prompt within a couple of minutes. A ticket works only once.

It ends with `Logged in as …`. The worker picks the token up on its next sync (within 15 minutes in the weigh-in window, 2 hours otherwise). The first sync downloads history and can take a while.

## Check it

- Admin dashboard: **Last sync** shows a time and no error.
- From the server: `sudo docker compose exec worker wp report reconcile --days 7` prints each day's weigh-ins next to the one the game used. Compare with the Garmin Connect app.

## When something goes wrong

While syncs fail, or no new data arrives for 36 hours, **markets wait: nothing settles on stale data**. The dashboard shows the reason, and an admin alert is posted once per failure streak (if Discord admin alerts are set up).

| Message | What to do |
| --- | --- |
| `no Garmin token yet: run garmin-login once` | Run `garmin-login`. |
| `Garmin login failed: the token expired or was revoked; rerun garmin-login` | Run `garmin-login` again (changing the Garmin password also revokes the token). |
| `GarminDB timed out after 15 min` | The first sync downloads history; later syncs continue where it stopped. If it keeps happening, check the network. |
| `no new data for N h` | Make sure the app or scale has synced to Garmin Connect. For long outages, void affected markets in Admin → Markets. |

## What is read

GarminDB downloads raw JSON into the `wp_garmin` volume; WeightPicks reads those files read-only:

- **Weigh-ins:** each entry with its real time, scale or manual. Weigh-ins count when they fall inside the weigh-in window (04:00–11:00 local by default).
- **Daily totals:** steps, active minutes, intensity minutes (moderate + 2 × vigorous) and active calories, once the day is over.
- **Workouts:** activities of 10 minutes or more.

Nothing is sent back to Garmin, and nothing from Garmin leaves the server (backups don't include the Garmin data or token).
