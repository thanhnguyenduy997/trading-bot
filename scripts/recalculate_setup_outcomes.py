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
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = TradeSetupOutcomeService(db).reconcile_historical_setups(
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
