from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.execution.base import AdapterError
from app.models.user import User
from app.schemas.trade_preview import TradePreviewRequest, TradePreviewResponse
from app.schemas.trade_setup import (
    ManualTradeSetupCreate,
    TradeEventRead,
    TradeSetupCreate,
    TradeSetupExecutionRead,
    TradeSetupMonitoringRead,
    TradeSetupReconciliationRead,
    TradeSetupRead,
)
from app.services.manual_trade_setups import ManualTradeSetupService
from app.services.preview_service import PreviewService
from app.services.trade_events import list_trade_events
from app.services.trade_setup_execution import TradeSetupExecutionService
from app.services.trade_setup_monitoring import TradeSetupMonitoringService
from app.services.trade_setup_outcomes import TradeSetupOutcomeService
from app.services.trade_setups import create_trade_setup, get_trade_setup, list_trade_setups


router = APIRouter(prefix="/api/trade-setups", tags=["trade-setups"])


@router.post("/preview", response_model=TradePreviewResponse)
def preview_trade_setup(
    payload: TradePreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradePreviewResponse:
    service = PreviewService(db)
    try:
        return service.build_preview(current_user.id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("", response_model=TradeSetupRead, status_code=status.HTTP_201_CREATED)
def create_trade_setup_endpoint(
    payload: TradeSetupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradeSetupRead:
    try:
        setup = create_trade_setup(db, current_user.id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return TradeSetupRead.model_validate(setup)


@router.post("/manual", response_model=TradeSetupRead, status_code=status.HTTP_201_CREATED)
def create_manual_trade_setup_endpoint(
    payload: ManualTradeSetupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradeSetupRead:
    try:
        setup = ManualTradeSetupService(db).create_manual_setup(user_id=current_user.id, payload=payload)
        TradeSetupOutcomeService(db).reconcile_setup(setup.id, current_user.id)
        setup = get_trade_setup(db, setup.id, current_user.id)
        if setup is None:
            raise LookupError("Trade setup not found")
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AdapterError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.to_dict()) from exc
    return TradeSetupRead.model_validate(setup)


@router.get("", response_model=List[TradeSetupRead])
def read_trade_setups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TradeSetupRead]:
    setups = list_trade_setups(db, current_user.id)
    return [TradeSetupRead.model_validate(setup) for setup in setups]


@router.get("/{setup_id}", response_model=TradeSetupRead)
def read_trade_setup(
    setup_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradeSetupRead:
    setup = get_trade_setup(db, setup_id, current_user.id)
    if not setup:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found")
    return TradeSetupRead.model_validate(setup)


@router.post("/{setup_id}/execute", response_model=TradeSetupExecutionRead)
def execute_trade_setup(
    setup_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradeSetupExecutionRead:
    service = TradeSetupExecutionService(db)
    try:
        setup = service.execute_setup(setup_id, current_user.id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AdapterError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.to_dict()) from exc
    return TradeSetupExecutionRead(
        success=True,
        status=setup.status,
        order1_ticket=setup.order1_ticket,
        order2_ticket=setup.order2_ticket,
        execution_error=setup.execution_error,
        execution_details=setup.execution_details,
        executed_at=setup.executed_at,
    )


@router.post("/{setup_id}/monitor", response_model=TradeSetupMonitoringRead)
def monitor_trade_setup(
    setup_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradeSetupMonitoringRead:
    service = TradeSetupMonitoringService(db)
    try:
        result = service.process_setup(setup_id, current_user.id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AdapterError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.to_dict()) from exc
    return TradeSetupMonitoringRead.model_validate(result)


@router.post("/{setup_id}/reconcile", response_model=TradeSetupReconciliationRead)
def reconcile_trade_setup(
    setup_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TradeSetupReconciliationRead:
    service = TradeSetupOutcomeService(db)
    try:
        result = service.reconcile_setup(setup_id, current_user.id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AdapterError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.to_dict()) from exc
    return TradeSetupReconciliationRead.model_validate(result)


@router.get("/{setup_id}/events", response_model=List[TradeEventRead])
def read_trade_setup_events(
    setup_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TradeEventRead]:
    setup = get_trade_setup(db, setup_id, current_user.id)
    if not setup:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade setup not found")
    events = list_trade_events(db, setup_id, current_user.id)
    return [TradeEventRead.model_validate(event) for event in events]
