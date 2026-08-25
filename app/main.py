import asyncio
import contextlib
import logging
import time
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import accounts as acc
from . import storage as st
from .admin_api import router as admin_router
from .amo import mock_deal, mock_writeback
from .config import settings
from .pipeline import handle_event, worker_tick
from .rules import evaluate, load_rules, load_yaml, set_provider
from .wazzup import mock_outbox, parse_webhook, wazzup

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

STATIC = Path(__file__).resolve().parent.parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    stop = asyncio.Event()

    # Первый запуск: аккаунт, администратор, правила из rules.yaml как начальные
    created = acc.bootstrap(load_yaml(force=True))
    if created:
        email, password = created
        log.warning("=" * 62)
        log.warning("Создан администратор.  Логин: %s   Пароль: %s", email, password)
        log.warning("Пароль показан один раз — сохраните его.")
        log.warning("=" * 62)
    acc.purge_sessions()
    # С этого момента правила читаются из базы, а не из файла
    set_provider(lambda: acc.get_rules(1))

    async def loop():
        while not stop.is_set():
            try:
                await worker_tick()
            except Exception:
                log.exception("worker tick failed")
            await asyncio.sleep(1)

    task = asyncio.create_task(loop())
    log.info("Режим: %s | воркер запущен", settings.MODE)
    yield
    stop.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="WhatsApp AI agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(admin_router)


@app.get("/admin")
async def admin_page():
    return FileResponse(STATIC / "admin.html")


# ----------------------------------------------------------------
# Вебхук Wazzup
# ----------------------------------------------------------------
@app.post("/webhook/wazzup")
async def wazzup_webhook(request: Request, bg: BackgroundTasks):
    """Отвечаем 200 немедленно, обработку уводим в фон.

    Требование Wazzup: быстрый ответ, бизнес-логика асинхронно.
    Тестовый POST {"test": true} при подписке тоже должен получить 200.
    """
    payload = await request.json()
    if payload.get("test"):
        return {"ok": True}
    for event in parse_webhook(payload):
        bg.add_task(_safe_handle, event)
    return {"ok": True}


async def _safe_handle(event: dict) -> None:
    try:
        await handle_event(event)
    except Exception:
        log.exception("event handling failed")


@app.get("/health")
async def health():
    pending = st.q("SELECT COUNT(*) AS c FROM jobs WHERE status = 'pending'")[0]["c"]
    return {"ok": True, "mode": settings.MODE, "pending_jobs": pending}


# ----------------------------------------------------------------
# Служебное: разовая настройка подключения
# ----------------------------------------------------------------
@app.get("/setup/channels")
async def setup_channels():
    return await wazzup.channels()


@app.get("/setup/webhooks")
async def setup_webhooks_get():
    return await wazzup.get_webhooks()


@app.post("/setup/webhooks")
async def setup_webhooks(request: Request):
    body = await request.json()
    return await wazzup.subscribe_webhooks(body["url"])


# ----------------------------------------------------------------
# Тестовая консоль
# ----------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(STATIC / "admin.html")


@app.get("/test")
async def test_page():
    return FileResponse(STATIC / "test.html")


@app.post("/test/message")
async def test_message(request: Request):
    """Имитирует входящее от клиента или реплику менеджера."""
    body = await request.json()
    chat_id = body.get("chat_id", "77011234567")
    author = body.get("author", "client")

    result = await handle_event({
        "kind": "message",
        "chat_id": chat_id,
        "chat_type": "whatsapp",
        "channel_id": "test-channel",
        "text": body.get("text", ""),
        "author": author,
        "phone": chat_id,
        "crm_user_id": None if author == "client" else "manager-1",
    })
    return result


@app.get("/test/state")
async def test_state(chat_id: str = "77011234567"):
    row = st.get_dialog(chat_id)
    dialog = dict(row) if row else {"state": "sleeping", "ai_message_count": 0}
    window_left = None
    if dialog.get("last_inbound_at"):
        hours = (load_rules().get("limits") or {}).get("window_hours", 24)
        window_left = max(0.0, hours - (time.time() - dialog["last_inbound_at"]) / 3600)
    pending = st.q(
        "SELECT COUNT(*) AS c FROM jobs WHERE status = 'pending'")[0]["c"]
    return {
        "dialog": dialog,
        "messages": st.history(chat_id),
        "decisions": st.decisions(chat_id, limit=8),
        "deal": mock_deal,
        "writeback": mock_writeback[-10:],
        "outbox": mock_outbox[-10:],
        "pending_jobs": pending,
        "window_hours_left": window_left,
    }


@app.post("/test/deal")
async def test_deal(request: Request):
    """Правка тестовой сделки: этап, теги, поля."""
    body = await request.json()
    for key in ("stage", "tags", "price", "responsible", "fields", "name"):
        if key in body:
            mock_deal[key] = body[key]
    return mock_deal


@app.post("/test/preview")
async def test_preview(request: Request):
    """Прогон правил без отправки — показывает вердикт и трассировку."""
    body = await request.json()
    chat_id = body.get("chat_id", "77011234567")
    row = st.get_dialog(chat_id)
    dialog = dict(row) if row else {"state": "sleeping", "ai_message_count": 0,
                                    "last_inbound_at": time.time()}
    decision = evaluate(dialog=dialog, deal=mock_deal,
                        message=body.get("text", ""),
                        history=st.history(chat_id))
    return decision.as_dict()


@app.get("/test/rules")
async def test_rules():
    return load_rules(force=True)


@app.post("/test/reset")
async def test_reset():
    st.reset_all()
    mock_outbox.clear()
    mock_writeback.clear()
    mock_deal.update({"stage": "Первичный контакт", "tags": [], "fields": {}})
    return {"ok": True}


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    log.exception("unhandled: %s", exc)
    return JSONResponse(status_code=500, content={"error": str(exc)})
