#!/usr/bin/env bash
# Release check: run the PREVIOUS release with simulated data, then upgrade it to the NEW
# release with scripts/upgrade.sh, and require the data to be intact.
#   .github/scripts/upgrade-test.sh 1.0.0-rc.1 1.0.0
set -euo pipefail
PREVIOUS="${1:?previous version}" NEW="${2:?new version}"
cd "$(dirname "$0")/../.."
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

umask 077
cat >.env <<ENV
WP_IMAGE=$DEFAULT_IMAGE
WP_VERSION=$PREVIOUS
WP_BIND=127.0.0.1:8000
WP_BASE_URL=http://127.0.0.1:8000
APP_ENV=dev
DATA_PROVIDER=simulated
WP_COOKIE_SECURE=false
APP_SECRET_KEY=$(head -c 48 /dev/urandom | base64 -w0)
ENV

snapshot() {
  compose exec -T worker python - <<'PY'
from sqlalchemy import func, select
from app.core.config import Settings
from app.core.db import make_engine
from app.models import Account, Bet, LedgerTxn, User
e = make_engine(Settings().db_url)
with e.connect() as c:
    rows = c.execute(select(User.email, Account.balance_cents, Account.pnl_cents)
                     .join(Account, Account.user_id == User.id).order_by(User.id)).all()
    txns = c.execute(select(func.count()).select_from(LedgerTxn)).scalar_one()
    bets = c.execute(select(func.count()).select_from(Bet)).scalar_one()
    settled = c.execute(select(func.count()).select_from(Bet).where(Bet.status != "open")).scalar_one()
print(f"txns={txns} bets={bets} settled={settled} " + " ".join(f"{u}:{b}/{p}" for u, b, p in rows))
PY
}

say "Starting $PREVIOUS with simulated data"
pull_images
compose up -d web worker
wait_healthy
compose exec -T worker python - <<'PY'
# Setup done (settings row), so drops, allowances and settlement all run.
from sqlalchemy import update
from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.db import immediate, make_engine
from app.models import InstanceSettingsRow
from app.services import instance
s = Settings(); e = make_engine(s.db_url)
with immediate(e) as c:
    instance.ensure(c, SystemClock(), s)
    c.execute(update(InstanceSettingsRow).values(setup_completed_at=SystemClock().now()))
PY
compose exec -T worker wp seed
compose exec -T worker wp sim advance --days 3 >/dev/null
compose exec -T worker python - <<'PY'
# Every player bets on every open side once, then a week plays out (settlements, allowances).
from sqlalchemy import select
from app.core.config import Settings
from app.core.db import make_engine
from app.models import Market, OddsVersion, Selection, User
from app.services.bets import BetRejected, place_bet
from app.services.sim import app_clock
s = Settings(); e = make_engine(s.db_url); clock = app_clock(s, e); now = clock.now()
with e.connect() as c:
    sides = c.execute(select(Selection.id, OddsVersion.id).join(Market, Market.id == Selection.market_id)
        .join(OddsVersion, (OddsVersion.market_id == Market.id) & OddsVersion.is_current)
        .where(Market.status == "open", Market.lock_at > now)).all()
    users = c.execute(select(User.id).where(User.role == "player")).scalars().all()
placed = 0
for u in users:
    for n, (sel, ver) in enumerate(sides):
        try:
            place_bet(e, clock, user_id=u, selection_id=sel, odds_version_id=ver,
                      stake_cents=500, client_key=f"upgrade-{u}-{n}")
            placed += 1
        except BetRejected:
            pass
print(f"placed {placed} bets")
assert placed > 0, "no open markets to bet on"
PY
compose exec -T worker wp sim advance --days 7 >/dev/null
before="$(snapshot)"
echo "before: $before"

./scripts/upgrade.sh --no-checkout "$NEW"

after="$(snapshot)"
echo "after:  $after"
test "$before" = "$after" || die "data changed during the upgrade"
image="$(compose ps --format '{{.Image}}' web)"
case "$image" in *":$NEW") ;; *) die "web runs $image, not $NEW" ;; esac
say "Upgrade $PREVIOUS -> $NEW kept every balance, bet and ledger entry"
