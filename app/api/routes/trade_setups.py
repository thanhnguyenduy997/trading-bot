from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.trade_preview import TradePreviewRequest, TradePreviewResponse
from app.services.preview_service import PreviewService


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
