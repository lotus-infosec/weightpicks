from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from sqlalchemy import Engine, insert, select, update

from app.core.clock import SimClock
from app.core.db import immediate
from app.models import Account, AuthAttempt, BannedEmail, Session, User
from app.services import auth, ledger
from app.services.auth import AuthError
from tests.integration.world import create_admin

PW = "correct horse battery"


@pytest.fixture
def code(migrated_engine: Engine, clock: SimClock) -> str:
    return auth.rotate_registration_code(migrated_engine, clock)


def register(engine: Engine, clock: SimClock, code: str, n: int = 1, **kw: str) -> int:
    args = {
        "email": f"Player{n}@Example.invalid",
        "display_name": f"Player {n}",
        "password": PW,
        "code": code,
        "ip": "10.0.0.1",
    } | kw
    return auth.register(engine, clock, **args)


def refused(fn: object) -> str:
    with pytest.raises(AuthError) as exc:
        fn()  # type: ignore[operator]
    return exc.value.code


# ---- registration -------------------------------------------------------------------------


def test_register_creates_player_with_grant(
    migrated_engine: Engine, clock: SimClock, code: str
) -> None:
    with immediate(migrated_engine) as conn:
        ledger.open_season(conn, clock)
    user_id = register(migrated_engine, clock, code)
    with migrated_engine.connect() as conn:
        user = conn.execute(select(User).where(User.id == user_id)).one()
        balance = conn.execute(
            select(Account.balance_cents).where(Account.user_id == user_id)
        ).scalar_one()
    assert (user.email, user.role, user.status) == ("player1@example.invalid", "player", "active")
    assert user.password_hash.startswith("$argon2id$") and PW not in user.password_hash
    assert balance == 100_000


def test_register_without_a_season_has_no_account_yet(
    migrated_engine: Engine, clock: SimClock, code: str
) -> None:
    user_id = register(migrated_engine, clock, code)
    with migrated_engine.connect() as conn:
        assert conn.execute(select(Account.id).where(Account.user_id == user_id)).first() is None


def test_registration_refusals(migrated_engine: Engine, clock: SimClock, code: str) -> None:
    e, c = migrated_engine, clock
    register(e, c, code)
    assert refused(lambda: register(e, c, "WRONG-CODE-0000", 2)) == "bad_code"
    assert (
        refused(lambda: register(e, c, code, 2, email="PLAYER1@example.invalid")) == "email_taken"
    )
    assert refused(lambda: register(e, c, code, 2, display_name="  player   1 ")) == "name_taken"
    with immediate(e) as conn:
        conn.execute(insert(BannedEmail).values(email="bad@example.invalid", banned_at=c.now()))
    assert (
        refused(lambda: register(e, c, code, 3, email="Bad@example.invalid", ip="10.0.0.2"))
        == "banned"
    )
    assert refused(lambda: register(e, c, code, 4, password="too short")) == "weak_password"
    assert refused(lambda: register(e, c, code, 4, email="no-at-sign")) == "invalid"
    assert refused(lambda: register(e, c, code, 4, display_name="x" * 41)) == "invalid"


def test_old_code_stops_working(migrated_engine: Engine, clock: SimClock, code: str) -> None:
    newer = auth.rotate_registration_code(migrated_engine, clock)
    assert refused(lambda: register(migrated_engine, clock, code)) == "bad_code"
    register(migrated_engine, clock, newer)


def test_register_rate_limit(migrated_engine: Engine, clock: SimClock, code: str) -> None:
    for n in range(5):
        register(migrated_engine, clock, code, n)
    assert refused(lambda: register(migrated_engine, clock, code, 9)) == "rate_limited"
    # Another network is unaffected; an hour later this one can register again.
    register(migrated_engine, clock, code, 10, ip="10.0.0.99")
    clock.advance(timedelta(hours=1, seconds=1))
    register(migrated_engine, clock, code, 11)


def test_register_rate_limit_counts_refusals(
    migrated_engine: Engine, clock: SimClock, code: str
) -> None:
    for n in range(5):
        refused(lambda n=n: register(migrated_engine, clock, "WRONG", n))
    assert refused(lambda: register(migrated_engine, clock, code, 9)) == "rate_limited"
    with migrated_engine.connect() as conn:
        assert len(conn.execute(select(AuthAttempt.id)).all()) == 6


# ---- login ----------------------------------------------------------------------------------


def login(
    engine: Engine,
    clock: SimClock,
    email: str = "player1@example.invalid",
    password: str = PW,
    ip: str = "10.0.0.1",
) -> auth.NewSession:
    return auth.login(engine, clock, email=email, password=password, ip=ip, user_agent="pytest")


def test_login_creates_a_hashed_session(
    migrated_engine: Engine, clock: SimClock, code: str
) -> None:
    user_id = register(migrated_engine, clock, code)
    new = login(migrated_engine, clock, email=" PLAYER1@example.invalid ")
    assert new.info.user_id == user_id and new.info.role == "player"
    assert new.info.expires_at == clock.now() + timedelta(days=30)
    with migrated_engine.connect() as conn:
        stored = conn.execute(select(Session.id_hash)).scalar_one()
    assert stored != new.token and len(stored) == 64
    info = auth.resolve(migrated_engine, clock, new.token)
    assert info is not None and info.csrf_token == new.info.csrf_token


def test_lockout_after_five_failures(migrated_engine: Engine, clock: SimClock, code: str) -> None:
    register(migrated_engine, clock, code)
    for _ in range(5):
        assert (
            refused(lambda: login(migrated_engine, clock, password="wrong password!"))
            == "bad_credentials"
        )
    assert refused(lambda: login(migrated_engine, clock)) == "rate_limited"  # even the right one
    login(migrated_engine, clock, ip="10.0.0.7")  # per (IP, email)
    clock.advance(timedelta(minutes=15, seconds=1))
    login(migrated_engine, clock)


def test_per_ip_limit(migrated_engine: Engine, clock: SimClock, code: str) -> None:
    register(migrated_engine, clock, code)
    for n in range(30):
        refused(lambda n=n: login(migrated_engine, clock, email=f"nobody{n}@example.invalid"))
    assert refused(lambda: login(migrated_engine, clock)) == "rate_limited"


def test_unknown_email_and_banned_user(migrated_engine: Engine, clock: SimClock, code: str) -> None:
    user_id = register(migrated_engine, clock, code)
    assert (
        refused(lambda: login(migrated_engine, clock, email="ghost@example.invalid"))
        == "bad_credentials"
    )
    new = login(migrated_engine, clock)
    with immediate(migrated_engine) as conn:
        conn.execute(update(User).where(User.id == user_id).values(status="banned"))
    assert auth.resolve(migrated_engine, clock, new.token) is None
    assert refused(lambda: login(migrated_engine, clock)) == "banned"


def test_frozen_players_can_still_log_in(
    migrated_engine: Engine, clock: SimClock, code: str
) -> None:
    user_id = register(migrated_engine, clock, code)
    with immediate(migrated_engine) as conn:
        conn.execute(update(User).where(User.id == user_id).values(status="frozen"))
    assert login(migrated_engine, clock).info.status == "frozen"


def test_rehash_on_login(migrated_engine: Engine, clock: SimClock, code: str) -> None:
    user_id = register(migrated_engine, clock, code)
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PW)
    with immediate(migrated_engine) as conn:
        conn.execute(update(User).where(User.id == user_id).values(password_hash=weak))
    login(migrated_engine, clock)
    with migrated_engine.connect() as conn:
        assert conn.execute(select(User.password_hash)).scalar_one() != weak


def test_sessions_slide_expire_and_revoke(
    migrated_engine: Engine, clock: SimClock, code: str
) -> None:
    user_id = register(migrated_engine, clock, code)
    new = login(migrated_engine, clock)
    clock.advance(timedelta(days=29))
    info = auth.resolve(migrated_engine, clock, new.token)
    assert info is not None and info.expires_at == clock.now() + timedelta(days=30)  # slid
    clock.advance(timedelta(days=30, seconds=1))
    assert auth.resolve(migrated_engine, clock, new.token) is None  # idle too long
    again = login(migrated_engine, clock)
    other = login(migrated_engine, clock)
    auth.logout(migrated_engine, again.token)
    assert auth.resolve(migrated_engine, clock, again.token) is None
    assert auth.resolve(migrated_engine, clock, other.token) is not None
    with immediate(migrated_engine) as conn:
        assert auth.revoke_all(conn, user_id) == 1
    assert auth.resolve(migrated_engine, clock, other.token) is None
    assert auth.resolve(migrated_engine, clock, None) is None
    assert auth.resolve(migrated_engine, clock, "x" * 200) is None


def test_admin_sessions_are_short(migrated_engine: Engine, clock: SimClock) -> None:
    admin = create_admin(
        migrated_engine, clock, email="admin@example.invalid", display_name="Admin", password=PW
    )
    new = login(migrated_engine, clock, email="admin@example.invalid")
    assert new.info.role == "admin" and new.info.user_id == admin
    assert new.info.expires_at == clock.now() + timedelta(hours=12)
    with migrated_engine.connect() as conn:
        assert conn.execute(select(Account.id).where(Account.user_id == admin)).first() is None
