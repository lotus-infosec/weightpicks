# Going live: the first weeks

WeightPicks was built and tested on a simulated clock and simulated data. These checks prove it on **your** machine, with real time and real Garmin data, before friends and family depend on it. They're the release gates from the build plan, condensed.

## Day 0: install

- [ ] `install.sh` finished; both containers healthy; `https://<your hostname>/healthz` returns 200 through the tunnel.
- [ ] `/setup` done; you can log in as the admin.
- [ ] `APP_SECRET_KEY` copied to a password manager.
- [ ] Garmin connected (`garmin-login`); the dashboard shows a successful sync.
- [ ] A backup made by hand and verified; an off-box copy set up (cron or systemd timer, see [backups.md](../backups.md)).
- [ ] Firewall: no inbound ports except SSH from your LAN.

## Week 1: just you (real time, real data)

- [ ] Markets drop every day at the drop time, lock at the lock time, and settle the next day after the weigh-in window, with no manual fixes.
- [ ] A day without a weigh-in pushes (refunds) instead of settling.
- [ ] Settled weight markets match what you calculate by hand from the Garmin app (`wp report reconcile --days 7` helps).
- [ ] One forced sync failure (for example, unplug the network for a sync) shows on the dashboard and as an admin alert, and nothing settles until data is back.
- [ ] Discord (if used): a test post reaches each channel.

## Weeks 2–3: a few trusted players

- [ ] 3–5 people sign up with the registration code and bet.
- [ ] Admin → System shows the ledger check clean every day.
- [ ] Any settlement question from a player is resolved within a day.
- [ ] The economy (bankroll, allowance, vig) feels right; adjust it in Admin → Bank.

## From then on

- [ ] A restore drill once: copy a backup to a spare machine, restore it, and compare the leaderboard.
- [ ] Turn on the extras when you want them (Admin → Settings): props and futures, parlays, special events, AI props.
- [ ] Watch the [Releases page](https://github.com/lotus-infosec/weightpicks/releases) and upgrade with `scripts/upgrade.sh`.
