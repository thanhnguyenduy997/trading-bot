from __future__ import annotations

import argparse
import json

from app.core.database import SessionLocal
from app.services.trade_setup_outcomes import TradeSetupOutcomeService


def main() -> None:
    parser = argparse.ArgumentParser(description="Recalculate setup outcomes using the current setup-level outcome engine.")
    parser.add_argument("--user-id", type=int, help="Restrict recalculation to one user.")
    parser.add_argument("--trading-account-id", type=int, help="Restrict recalculation to one trading account.")
    parser.add_argument("--setup-id", type=int, action="append", dest="setup_ids", help="Recalculate one or more specific setup ids.")
    parser.add_argument("--limit", type=int, help="Optional limit for batch recalculation.")
    parser.add_argument("--mode", choices=["stored", "live"], default="stored", help="Use stored setup/order data only, or run full live MT5 reconciliation.")
    parser.add_argument("--all", action="store_true", help="Run against all accounts when no narrower filter is provided.")
    args = parser.parse_args()

    if not args.all and args.user_id is None and args.trading_account_id is None and not args.setup_ids:
        parser.error("Provide --all or at least one of --user-id, --trading-account-id, or --setup-id.")

    db = SessionLocal()
    try:
        service = TradeSetupOutcomeService(db)
        if args.mode == "stored":
            result = service.backfill_setup_outcomes_from_stored_data(
                user_id=args.user_id,
                trading_account_id=args.trading_account_id,
                setup_ids=args.setup_ids,
            )
        else:
            result = service.reconcile_historical_setups(
                user_id=args.user_id,
                trading_account_id=args.trading_account_id,
                setup_ids=args.setup_ids,
                limit=args.limit,
            )
        print(json.dumps(result, default=str, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
