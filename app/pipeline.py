"""Оркестратор.

Состояния диалога:
    sleeping    — первичка ушла, клиент молчит. ИИ не активен.
    armed       — клиент ответил, окно открыто. ИИ решает по правилам.
    active      — ИИ ведёт диалог.
    handed_off  — вмешался менеджер или сработал триггер передачи.
"""
import logging
import time

from . import storage as st
from .amo import amo
from .config import settings
from . import accounts as acc
from .llm import generate
from .rules import Decision, evaluate, load_rules
from .wazzup import classify_author, wazzup

log = logging.getLogger("pipeline")


async def handle_event(event: dict) -> dict:
    """Точка входа для любого события из вебхука или тестового чата."""
    kind = event.get("kind")
    if kind != "message":
        return {"skipped": kind}

    chat_id = event.get("chat_id")
    if not chat_id:
        return {"skipped": "no chat_id"}

    author = event.get("author") or classify_author(event)
    text = (event.get("text") or "").strip()
    now = time.time()

    contact = event.get("contact") or {}
    phone = contact.get("phone") or event.get("phone") or chat_id

    dialog = st.upsert_dialog(chat_id, channel_id=event.get("channel_id"), phone=phone)
    dialog = dict(dialog)

    # --- Исходящее от менеджера: немедленный перехват ---
    if author == "manager":
        st.add_message(chat_id, "outbound", "manager", text, event.get("crm_user_id"))
        st.upsert_dialog(chat_id, state="handed_off", handed_off_at=now, last_outbound_at=now)
        st.add_decision(chat_id, "handoff", "Менеджер написал сам — ИИ отключён", [])
        return {"action": "manager_takeover"}

    # --- Наше собственное исходящее: только фиксируем ---
    if author == "ai":
        st.add_message(chat_id, "outbound", "ai", text, event.get("crm_user_id"))
        st.upsert_dialog(chat_id, last_outbound_at=now)
        return {"action": "own_echo"}

    # --- Входящее от клиента ---
    st.add_message(chat_id, "inbound", "client", text)
    fields = {"last_inbound_at": now, "followup_count": 0}
    if not dialog.get("window_opened_at"):
        fields["window_opened_at"] = now
    if dialog.get("state") == "sleeping":
        fields["state"] = "armed"
    st.upsert_dialog(chat_id, **fields)

    dialog = dict(st.get_dialog(chat_id))
    history = st.history(chat_id)
    deal = await amo.get_deal_context(phone)

    decision = evaluate(dialog=dialog, deal=deal, message=text, history=history)
    st.add_decision(chat_id, decision.verdict, decision.reason,
                    [c.as_dict() for c in decision.checks])

    if decision.verdict == "handoff":
        await _do_handoff(chat_id, deal, decision)
        return {"action": "handoff", "decision": decision.as_dict()}

    if decision.verdict == "queue":
        st.enqueue({"type": "reply", "chat_id": chat_id, "phone": phone}, delay=_until_workday())
        return {"action": "queued", "decision": decision.as_dict()}

    if decision.verdict != "reply":
        return {"action": "silent", "decision": decision.as_dict()}

    # Пауза перед ответом — окно для перехвата менеджером
    st.enqueue({"type": "reply", "chat_id": chat_id, "phone": phone},
               delay=settings.REPLY_DELAY_SECONDS)
    return {"action": "scheduled", "decision": decision.as_dict(),
            "delay": settings.REPLY_DELAY_SECONDS}


async def send_reply(chat_id: str, phone: str) -> dict:
    """Выполняется воркером после паузы. Правила проверяются повторно —
    за время паузы менеджер мог перехватить диалог."""
    row = st.get_dialog(chat_id)
    if row is None:
        return {"skipped": "no dialog"}
    dialog = dict(row)

    if dialog.get("state") == "handed_off":
        st.add_decision(chat_id, "silent", "За время паузы вмешался менеджер", [])
        return {"skipped": "handed_off"}

    history = st.history(chat_id)
    deal = await amo.get_deal_context(phone)
    last_client = next((m["text"] for m in reversed(history) if m["author"] == "client"), "")

    decision = evaluate(dialog=dialog, deal=deal, message=last_client, history=history)
    if not decision.should_reply:
        st.add_decision(chat_id, decision.verdict,
                        f"Повторная проверка: {decision.reason}",
                        [c.as_dict() for c in decision.checks])
        if decision.verdict == "handoff":
            await _do_handoff(chat_id, deal, decision)
        return {"skipped": decision.verdict}

    prompt = acc.get_prompt(dialog.get('account_id') or 1)
    text = await generate(history, deal, goal=prompt.get('goal', ''),
                          kb=prompt.get('knowledge', ''), style=prompt)
    await wazzup.send_text(chat_id, text, channel_id=dialog.get("channel_id"))

    st.add_message(chat_id, "outbound", "ai", text, settings.AI_CRM_USER_ID)
    st.upsert_dialog(chat_id, state="active", last_outbound_at=time.time())
    st.bump(chat_id, "ai_message_count")

    rules = load_rules()
    wb = rules.get("amo_writeback") or {}
    if wb.get("add_note") and deal.get("id"):
        await amo.add_note(int(deal["id"]), f"ИИ ответил клиенту:\n{text}")

    return {"sent": text}


async def _do_handoff(chat_id: str, deal: dict, decision: Decision) -> None:
    st.upsert_dialog(chat_id, state="handed_off", handed_off_at=time.time())
    rules = load_rules()
    wb = rules.get("amo_writeback") or {}
    notify = (rules.get("handoff") or {}).get("notify") or {}
    lead_id = deal.get("id")
    if lead_id:
        if wb.get("set_tag_on_handoff"):
            await amo.add_tag(int(lead_id), wb["set_tag_on_handoff"])
        if notify.get("amo_task"):
            await amo.create_task(int(lead_id),
                                  notify.get("task_text") or decision.reason)
        if wb.get("add_note"):
            await amo.add_note(int(lead_id), f"ИИ передал диалог: {decision.reason}")


def _until_workday() -> float:
    """Секунды до начала следующего рабочего окна."""
    from datetime import datetime, timedelta, time as dtime
    from zoneinfo import ZoneInfo

    sched = (load_rules().get("schedule") or {})
    tz = ZoneInfo(sched.get("timezone", settings.TZ))
    start = dtime.fromisoformat(sched.get("start", "09:00"))
    workdays = sched.get("workdays", [1, 2, 3, 4, 5])
    now = datetime.now(tz)
    candidate = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    for _ in range(8):
        if candidate.isoweekday() in workdays:
            break
        candidate += timedelta(days=1)
    return max(60.0, (candidate - now).total_seconds())


async def worker_tick() -> int:
    """Один проход воркера по очереди."""
    jobs = st.claim_jobs()
    for job in jobs:
        payload = job["payload"]
        try:
            if payload.get("type") == "reply":
                await send_reply(payload["chat_id"], payload.get("phone", ""))
            st.finish_job(job["id"], ok=True)
        except Exception as exc:
            log.exception("job %s failed: %s", job["id"], exc)
            st.finish_job(job["id"], ok=False)
    return len(jobs)
