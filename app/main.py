from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_settings
from app.services.background_monitor import TradeMonitorRunner


settings = get_settings()
trade_monitor = TradeMonitorRunner(settings)

app = FastAPI(title=settings.app_name, debug=settings.debug)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory="app/templates/static"), name="static")


@app.on_event("startup")
async def start_trade_monitor() -> None:
    trade_monitor.start()


@app.on_event("shutdown")
async def stop_trade_monitor() -> None:
    await trade_monitor.stop()
