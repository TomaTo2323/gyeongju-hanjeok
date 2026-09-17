from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import router
from .auth_api import auth_router
from .compat_api import compat_router
from .community_api import community_router
from .config import get_settings
from .db import SessionLocal, init_db
from .friend_api import friend_router, invite_landing_router
from .shared_route_api import shared_route_router, shared_route_landing_router
from .services import SyncService

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Seoul")


async def scheduled_sync() -> None:
    if not settings.public_data_service_key:
        logger.warning("?쇱씪 ?숆린?붾? 嫄대꼫?곷땲?? PUBLIC_DATA_SERVICE_KEY 誘몄꽕??)
        return
    db = SessionLocal()
    try:
        result = await SyncService(settings, db).sync()
        logger.info("?쇱씪 愿愿묒? ?숆린???꾨즺: %s", result.model_dump())
    except Exception:
        logger.exception("?쇱씪 愿愿묒? ?숆린???ㅽ뙣")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.daily_sync_enabled:
        scheduler.add_job(scheduled_sync, "cron", hour=settings.daily_sync_hour, minute=0, id="daily-tour-sync", replace_existing=True)
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


from .companion_request_api import companion_router
from .notification_api import notification_router
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="?쇱옟?꾨? ?듭떖 ?쒖빟議곌굔?쇰줈 ?ъ슜?섎뒗 寃쎌＜ ?ㅼ떆媛?留욎땄 愿愿?肄붿뒪 API",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(auth_router)
app.include_router(friend_router)
app.include_router(invite_landing_router)
app.include_router(shared_route_router)
app.include_router(shared_route_landing_router)
app.include_router(compat_router)
app.include_router(community_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_SERVER_ERROR", "message": "?쒕쾭 ?대? ?ㅻ쪟媛 諛쒖깮?덉뒿?덈떎.", "details": str(exc) if settings.app_env == "development" else None}})

app.include_router(companion_router)
app.include_router(notification_router)
