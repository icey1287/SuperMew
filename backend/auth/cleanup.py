"""Scheduled retention cleanup for the durable refresh-token ledger."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from backend.core.settings import SecuritySettings, get_settings
from backend.db.models import RefreshToken
from backend.infra.database import SessionLocal


DEFAULT_BATCH_SIZE = 1_000
DEFAULT_MAX_BATCHES = 100


def _db_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def purge_refresh_token_batch(
    db: Session,
    *,
    settings: SecuritySettings,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Delete one bounded batch only after natural expiry plus retention."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    current = _db_utc(now or datetime.now(UTC))
    cutoff = current - timedelta(days=settings.refresh_token_retention_days)
    candidates = [
        token_id
        for (token_id,) in (
            db.query(RefreshToken.id)
            .filter(RefreshToken.expires_at <= cutoff)
            .order_by(RefreshToken.expires_at, RefreshToken.id)
            .limit(batch_size)
            .all()
        )
    ]
    if not candidates:
        return 0
    try:
        deleted = int(
            db.query(RefreshToken)
            .filter(RefreshToken.id.in_(candidates))
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted
    except BaseException:
        db.rollback()
        raise


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Purge refresh-token ledger rows only after token expiry and the "
            "configured replay-detection retention window."
        )
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=DEFAULT_MAX_BATCHES,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    if args.batch_size <= 0 or args.max_batches <= 0:
        raise SystemExit("--batch-size and --max-batches must be positive")

    settings = get_settings().security
    total = 0
    for _ in range(args.max_batches):
        with SessionLocal() as db:
            deleted = purge_refresh_token_batch(
                db,
                settings=settings,
                batch_size=args.batch_size,
            )
        total += deleted
        if deleted < args.batch_size:
            break

    print(f"purged_refresh_tokens={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["purge_refresh_token_batch"]
