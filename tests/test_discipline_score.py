from datetime import datetime, timedelta, timezone

from app.models.mt5_trade_history import MT5TradeHistory
from app.models.risk_control_log import RiskControlLog
from app.models.trade_event import TradeEvent
from app.schemas.trade_setup import TradeSetupCreate
from app.schemas.trading_account import TradingAccountCreate
from app.services.discipline_score import DisciplineScoreService
from app.services.trade_setups import create_trade_setup
from app.services.trading_accounts import create_trading_account


RANGE_START = datetime(2026, 5, 1, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 5, 3, 23, 59, tzinfo=timezone.utc)


def _create_account(db_session, user, account_number: str = "DISC-001"):
    return create_trading_account(
        db_session,
        user.id,
        TradingAccountCreate(
            broker_name="Demo Broker",
            account_number=account_number,
            server_name="demo-server",
            password="secret-pass",
        ),
    )


def _create_setup(
    db_session,
    user,
    account,
    *,
    setup_outcome: str | None = "full_win",
    when: datetime = datetime(2026, 5, 2, 10, tzinfo=timezone.utc),
    status: str = "executed",
):
    setup = create_trade_setup(
        db_session,
        user.id,
        TradeSetupCreate(
            trading_account_id=account.id,
            symbol="XAUUSD",
            side="buy",
            sl_price=2319.2,
            risk_mode="fixed_money",
            risk_value=100,
            rr_order2=2,
            estimated_entry=2320.2,
            r_value=1.0,
            tp1_price=2321.2,
            tp2_price=2322.2,
            total_risk_money=100.0,
            risk_per_order=50.0,
            order1_volume=0.5,
            order2_volume=0.5,
            status="draft",
        ),
    )
    setup.status = status
    setup.setup_outcome = setup_outcome
    setup.setup_outcome_recorded_at = when if setup_outcome else None
    setup.executed_at = when - timedelta(minutes=30)
    setup.order1_closed_at = when
    setup.order2_closed_at = when
    db_session.add(setup)
    db_session.commit()
    db_session.refresh(setup)
    return setup


def _score(db_session, user, account):
    return DisciplineScoreService(db_session).compute(
        actor=user,
        selected_account=account,
        range_start=RANGE_START,
        range_end=RANGE_END,
    )


def _login(client, email: str, password: str = "password123"):
    return client.post("/api/auth/login", data={"email": email, "password": password}, follow_redirects=False)


def _category(score, key: str):
    return next(category for category in score.categories if category.key == key)


def test_perfect_clean_range_scores_100(db_session, created_user):
    account = _create_account(db_session, created_user)
    _create_setup(db_session, created_user, account, setup_outcome="full_win")

    score = _score(db_session, created_user, account)

    assert score.total_score == 100
    assert score.status_label == "Rất kỷ luật"
    assert score.deductions == []


def test_unlinked_manual_trade_reduces_entry_and_process(db_session, created_user):
    account = _create_account(db_session, created_user)
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            position_ticket=7001,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            outcome="take_profit",
            volume=0.5,
            open_price=2320.0,
            close_price=2321.0,
            realized_pnl=10.0,
            open_time=datetime(2026, 5, 2, 9, tzinfo=timezone.utc),
            close_time=datetime(2026, 5, 2, 10, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    score = _score(db_session, created_user, account)

    assert _category(score, "entry").score == 25
    assert _category(score, "process").score == 12
    assert score.total_score == 92


def test_scratch_manual_reduces_management(db_session, created_user):
    account = _create_account(db_session, created_user)
    _create_setup(db_session, created_user, account, setup_outcome="scratch_manual")

    score = _score(db_session, created_user, account)

    assert _category(score, "management").score == 21
    assert any(deduction.code == "scratch_manual_setup" and deduction.points == 4 for deduction in score.deductions)


def test_review_required_reduces_management(db_session, created_user):
    account = _create_account(db_session, created_user)
    _create_setup(db_session, created_user, account, setup_outcome="review_required")

    score = _score(db_session, created_user, account)

    assert _category(score, "management").score == 20
    assert any(deduction.code == "review_required_setup" and deduction.points == 5 for deduction in score.deductions)


def test_blocked_trade_attempt_reduces_risk(db_session, created_user):
    account = _create_account(db_session, created_user)
    setup = _create_setup(db_session, created_user, account, setup_outcome="full_loss")
    db_session.add(
        RiskControlLog(
            user_id=created_user.id,
            setup_id=setup.id,
            event_type="daily_lock_execute_rejected",
            message="blocked",
            created_at=datetime(2026, 5, 2, 11, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    score = _score(db_session, created_user, account)

    assert _category(score, "risk").score == 15
    assert any(deduction.code == "daily_lock_trade_attempt_entry" for deduction in score.deductions)


def test_missing_outcome_reduces_process(db_session, created_user):
    account = _create_account(db_session, created_user)
    _create_setup(db_session, created_user, account, setup_outcome=None)

    score = _score(db_session, created_user, account)

    assert _category(score, "process").score == 12
    assert score.total_score == 97


def test_total_score_uses_clamped_subscores(db_session, created_user):
    account = _create_account(db_session, created_user)
    for index in range(10):
        db_session.add(
            MT5TradeHistory(
                user_id=created_user.id,
                trading_account_id=account.id,
                position_ticket=8000 + index,
                symbol="XAUUSD",
                side="buy",
                trade_source="manual",
                outcome="stoploss",
                volume=0.5,
                open_price=2320.0,
                close_price=2319.0,
                realized_pnl=-10.0,
                open_time=datetime(2026, 5, 2, 9, tzinfo=timezone.utc),
                close_time=datetime(2026, 5, 2, 10, tzinfo=timezone.utc),
            )
        )
    db_session.commit()

    score = _score(db_session, created_user, account)

    assert _category(score, "entry").score == 0
    assert _category(score, "process").score == 0
    assert score.total_score == sum(category.score for category in score.categories)


def test_selected_date_range_affects_score(db_session, created_user):
    account = _create_account(db_session, created_user)
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            position_ticket=9001,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            outcome="take_profit",
            volume=0.5,
            open_price=2320.0,
            close_price=2321.0,
            realized_pnl=10.0,
            open_time=datetime(2026, 4, 20, 9, tzinfo=timezone.utc),
            close_time=datetime(2026, 4, 20, 10, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    score = _score(db_session, created_user, account)

    assert score.total_score == 100


def test_selected_trading_account_scope_affects_score(db_session, created_user):
    clean_account = _create_account(db_session, created_user, "DISC-CLEAN")
    penalized_account = _create_account(db_session, created_user, "DISC-PEN")
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=penalized_account.id,
            position_ticket=9101,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            outcome="take_profit",
            volume=0.5,
            open_price=2320.0,
            close_price=2321.0,
            realized_pnl=10.0,
            open_time=datetime(2026, 5, 2, 9, tzinfo=timezone.utc),
            close_time=datetime(2026, 5, 2, 10, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    assert _score(db_session, created_user, clean_account).total_score == 100
    assert _score(db_session, created_user, penalized_account).total_score == 92


def test_score_status_label_mapping(db_session):
    service = DisciplineScoreService(db_session)

    assert service.status_label(95) == "Rất kỷ luật"
    assert service.status_label(82) == "Tốt"
    assert service.status_label(65) == "Cần siết lại"
    assert service.status_label(40) == "Mất kỷ luật"


def test_accepted_excessive_drift_reduces_risk(db_session, created_user):
    account = _create_account(db_session, created_user)
    setup = _create_setup(db_session, created_user, account)
    db_session.add(
        TradeEvent(
            user_id=created_user.id,
            setup_id=setup.id,
            event_type="preview_drift_accept_execute",
            message="accepted",
            created_at=datetime(2026, 5, 2, 11, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    score = _score(db_session, created_user, account)

    assert _category(score, "risk").score == 27


def test_dashboard_renders_discipline_score_section(client, db_session, created_user):
    account = _create_account(db_session, created_user)
    db_session.add(
        MT5TradeHistory(
            user_id=created_user.id,
            trading_account_id=account.id,
            position_ticket=9201,
            symbol="XAUUSD",
            side="buy",
            trade_source="manual",
            outcome="take_profit",
            volume=0.5,
            open_price=2320.0,
            close_price=2321.0,
            realized_pnl=10.0,
            open_time=datetime(2026, 5, 2, 9, tzinfo=timezone.utc),
            close_time=datetime(2026, 5, 2, 10, tzinfo=timezone.utc),
        )
    )
    db_session.commit()
    _login(client, created_user.email)

    response = client.get(
        f"/dashboard?account_id={account.id}&range=custom&start_date=2026-05-01&end_date=2026-05-03"
    )

    assert response.status_code == 200
    assert "Discipline Score" in response.text
    assert "<strong>92</strong>" in response.text
    assert "Unlinked manual trade" in response.text
