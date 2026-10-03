"""SQLAlchemy tables. Import from here so Alembic sees every table."""

from app.models.auth import AuthAttempt, BannedEmail, RegistrationCode, Session
from app.models.base import Base
from app.models.bets import Bet, BetLeg, Settlement
from app.models.ledger import Account, LedgerEntry, LedgerTxn, Season, User
from app.models.markets import Market, OddsVersion, Selection
from app.models.observations import Observation, SimState, SyncRun
from app.models.outbox import OutboxMessage
from app.models.settings import InstanceSettingsRow
from app.models.system import Heartbeat, JobRun

__all__ = [
    "Account",
    "AuthAttempt",
    "BannedEmail",
    "Base",
    "Bet",
    "BetLeg",
    "Heartbeat",
    "InstanceSettingsRow",
    "JobRun",
    "LedgerEntry",
    "LedgerTxn",
    "Market",
    "Observation",
    "OddsVersion",
    "OutboxMessage",
    "RegistrationCode",
    "Season",
    "Selection",
    "Session",
    "Settlement",
    "SimState",
    "SyncRun",
    "User",
]
