from datetime import datetime, timedelta

import structlog

from app.services import ledger
from app.worker.registry import JobContext

log = structlog.get_logger()
RUN_HOUR_UTC = 3  # 03:00 until the instance time zone exists (STAGE08)


class LedgerMismatch(Exception):
    pass


class LedgerVerifyJob:
    """Nightly proof that cached balances and P&L equal the ledger entries."""

    name = "ledger_verify"

    def due(self, now: datetime) -> str:
        # Key by the most recent 03:00 so a run missed during downtime happens on restart.
        anchor = now if now.hour >= RUN_HOUR_UTC else now - timedelta(days=1)
        return anchor.date().isoformat()

    def run(self, ctx: JobContext) -> None:
        with ctx.engine.connect() as conn:
            report = ledger.verify(conn)
        if not report.ok:
            log.error(
                "ledger_mismatch",
                accounts=[m.account_id for m in report.mismatches],
                unbalanced_txns=report.unbalanced_txn_ids,
                negative_players=report.negative_player_account_ids,
            )
            raise LedgerMismatch(
                f"{len(report.mismatches)} account mismatches, "
                f"{len(report.unbalanced_txn_ids)} unbalanced txns, "
                f"{len(report.negative_player_account_ids)} negative players"
            )
        log.info("ledger_ok", accounts=report.accounts_checked, txns=report.txns_checked)
