from fastapi import APIRouter

from app.api.routes import auth, pages, trade_setups, trading_accounts


api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(trading_accounts.router)
api_router.include_router(trade_setups.router)
api_router.include_router(pages.router)
