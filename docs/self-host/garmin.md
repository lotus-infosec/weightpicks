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
- Admin → System: **Watch last synced** shows when the watch last uploaded to Garmin Connect, and which device.
- From the server: `sudo docker compose exec worker wp report reconcile --days 7` prints each day's weigh-ins next to the one the game used. Compare with the Garmin Connect app.

## The watch has to sync

An iPhone doesn't sync Garmin Connect in the background: until the app is opened, Garmin's own servers may hold only part of the day (12 steps instead of 12,000). So each sync also asks Garmin when the watch last uploaded, and a day's steps, minutes, calories and workouts count only once the watch has synced **at or after the cutoff that evening** (22:00 by default).

- **Open Garmin Connect before bed.** A reminder goes to the Admin alerts webhook at the cutoff. Steps after that last sync don't count for the day.
- **Forgot?** The day's markets wait. Every sync checks again until the grace period runs out (12 hours after 02:00, so 14:00 by default). Opening the app in the morning (for the weigh-in) still settles them on the full day.
- **No sync by then** (a trip without signal, a dead phone): the day's markets push, every stake is refunded, and the admin gets one alert for the day. The day's totals still fill in on Stats, and in the odds, once the watch syncs.
- If Garmin can't be reached at all, the sync fails and markets wait instead of pushing.

Change the cutoff and the grace period (0–48 hours) in **Admin → Settings → Garmin watch sync**.

**Sync now** on the admin dashboard re-reads the last 30 days (scheduled syncs only look at the last week). Use it after a long trip or whenever a day on Stats looks wrong: any day whose total changed is corrected, and missed workouts are added. Settled bets never change.

## When something goes wrong

While syncs fail, or no new data arrives for 36 hours, **markets wait: nothing settles on stale data**. (A sync that heard from Garmin about the watch doesn't count as stale: the watch just hasn't uploaded, and its days push at the grace deadline.) The dashboard shows the reason, and an admin alert is posted once per failure streak (if Discord admin alerts are set up).

| Message | What to do |
| --- | --- |
| `no Garmin token yet: run garmin-login once` | Run `garmin-login`. |
| `Garmin login failed: the token expired or was revoked; rerun garmin-login` | Run `garmin-login` again (changing the Garmin password also revokes the token). |
| `GarminDB timed out after 15 min` | The first sync downloads history; later syncs continue where it stopped. If it keeps happening, check the network. |
| `no new data for N h` | Make sure the app or scale has synced to Garmin Connect. For long outages, void affected markets in Admin → Markets. |

## What is read

GarminDB downloads raw JSON into the `wp_garmin` volume, and a small fetch with the same token adds the latest weigh-ins, the watch's last upload and the last week's daily summaries. WeightPicks reads those files read-only:

- **Weigh-ins:** each entry with its real time, scale or manual. Weigh-ins count when they fall inside the weigh-in window (04:00–11:00 local by default).
- **Daily totals:** steps, active minutes, intensity minutes (moderate + 2 × vigorous) and active calories, once the day is over and the watch has synced (see above). If Garmin later reports a different total for a stored day, the new total is kept as a correction: Stats and the odds use it, but markets already settled don't change.
- **The watch's last upload:** its time and device name, nothing else.
- **Workouts:** activities of 10 minutes or more.

Nothing is sent back to Garmin, and nothing from Garmin leaves the server (backups don't include the Garmin data or token).
