"""API административной панели."""
import time
from typing import Any, Optional

from fastapi import APIRouter, Cookie, HTTPException, Request, Response

from . import accounts as acc
from . import storage as st
from .amo import amo
from .rules import evaluate, load_rules

router = APIRouter(prefix="/api")

COOKIE = "wa_session"


def me(token: Optional[str]) -> dict:
    user = acc.user_by_session(token or "")
    if not user:
        raise HTTPException(401, "Нужно войти")
    return user


# ---------------- авторизация ----------------

@router.post("/login")
async def login(request: Request, response: Response):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    user = acc.user_by_email(email)
    if not user or not acc.verify_password(body.get("password") or "", user["pwd_hash"]):
        raise HTTPException(401, "Неверная почта или пароль")
    token = acc.create_session(user["id"])
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                        max_age=14 * 86400, path="/")
    acc.log(user["account_id"], user["email"], "auth.login", {})
    return {"email": user["email"], "name": user["name"], "role": user["role"]}


@router.post("/logout")
async def logout(response: Response, wa_session: str = Cookie(default="")):
    acc.drop_session(wa_session)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def whoami(wa_session: str = Cookie(default="")):
    u = me(wa_session)
    return {"email": u["email"], "name": u["name"], "role": u["role"],
            "account_id": u["account_id"]}


# ---------------- правила ----------------

@router.get("/rules")
async def rules_get(wa_session: str = Cookie(default="")):
    u = me(wa_session)
    return acc.get_rules(u["account_id"])


@router.put("/rules")
async def rules_put(request: Request, wa_session: str = Cookie(default="")):
    u = me(wa_session)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Ожидается объект настроек")
    errors = validate_rules(body)
    if errors:
        raise HTTPException(400, "; ".join(errors))
    acc.save_rules(u["account_id"], body, u["email"])
    return {"ok": True, "saved_at": time.time()}


def validate_rules(r: dict) -> list[str]:
    """Проверка перед сохранением: сломанные правила молчат опаснее, чем ошибка."""
    errs: list[str] = []
    stages = r.get("stages") or {}
    if stages.get("mode") not in {"allowlist", "blocklist"}:
        errs.append("Режим этапов должен быть allowlist или blocklist")
    if stages.get("mode") == "allowlist" and not (stages.get("values") or []):
        errs.append("В режиме allowlist нужен хотя бы один этап, иначе ИИ будет молчать всегда")

    sched = r.get("schedule") or {}
    for key in ("start", "end"):
        val = str(sched.get(key, ""))
        parts = val.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            errs.append(f"Время «{key}» должно быть в формате ЧЧ:ММ")
    if not (sched.get("workdays") or []):
        errs.append("Нужен хотя бы один рабочий день")

    limits = r.get("limits") or {}
    if int(limits.get("window_hours") or 24) > 24:
        errs.append("Окно WhatsApp не может превышать 24 часа")
    if int(limits.get("max_ai_messages_per_dialog") or 0) < 1:
        errs.append("Лимит реплик ИИ должен быть не меньше 1")

    manual = r.get("manual_switch") or {}
    on, off = manual.get("on_tag"), manual.get("off_tag")
    if on and off and on == off:
        errs.append("Теги включения и выключения не могут совпадать")

    stop_tags = {str(t).lower() for t in (r.get("stop_list") or {}).get("tags") or []}
    if on and str(on).lower() in stop_tags:
        errs.append("Тег включения не может быть в стоп-листе")
    return errs


@router.post("/rules/preview")
async def rules_preview(request: Request, wa_session: str = Cookie(default="")):
    """Прогон правил на выдуманной ситуации — без отправки чего-либо."""
    me(wa_session)
    body = await request.json()
    dialog = {
        "state": body.get("state", "armed"),
        "ai_message_count": int(body.get("ai_message_count") or 0),
        "last_inbound_at": time.time() - float(body.get("hours_since_reply") or 0) * 3600,
        "last_outbound_at": 0,
        "phone": "77000000000",
    }
    deal = {
        "id": 0,
        "stage": body.get("stage") or "",
        "tags": body.get("tags") or [],
        "fields": body.get("fields") or {},
        "phone": "77000000000",
    }
    return evaluate(dialog=dialog, deal=deal,
                    message=body.get("text") or "", history=[]).as_dict()


# ---------------- промпт и база знаний ----------------

@router.get("/prompt")
async def prompt_get(wa_session: str = Cookie(default="")):
    u = me(wa_session)
    return acc.get_prompt(u["account_id"])


@router.put("/prompt")
async def prompt_put(request: Request, wa_session: str = Cookie(default="")):
    u = me(wa_session)
    body = await request.json()
    acc.save_prompt(u["account_id"], body, u["email"])
    return {"ok": True}


# ---------------- диалоги ----------------

@router.get("/dialogs")
async def dialogs_list(state: str = "", q: str = "", limit: int = 50,
                       wa_session: str = Cookie(default="")):
    me(wa_session)
    sql = "SELECT * FROM dialogs WHERE 1=1"
    params: list[Any] = []
    if state:
        sql += " AND state = ?"
        params.append(state)
    if q:
        sql += " AND (phone LIKE ? OR chat_id LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY COALESCE(last_inbound_at, 0) DESC LIMIT ?"
    params.append(min(limit, 200))

    out = []
    for row in st.q(sql, tuple(params)):
        d = dict(row)
        last = st.q(
            "SELECT text, author FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (d["chat_id"],))
        dec = st.q(
            "SELECT verdict, reason FROM decisions WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (d["chat_id"],))
        d["last_message"] = dict(last[0]) if last else None
        d["last_decision"] = dict(dec[0]) if dec else None
        out.append(d)
    return {"items": out}


@router.get("/dialogs/{chat_id}")
async def dialog_detail(chat_id: str, wa_session: str = Cookie(default="")):
    me(wa_session)
    row = st.get_dialog(chat_id)
    if row is None:
        raise HTTPException(404, "Диалог не найден")
    dialog = dict(row)
    deal = await amo.get_deal_context(dialog.get("phone") or chat_id)
    window_left = None
    if dialog.get("last_inbound_at"):
        hours = (load_rules().get("limits") or {}).get("window_hours", 24)
        window_left = max(0.0, hours - (time.time() - dialog["last_inbound_at"]) / 3600)
    return {
        "dialog": dialog,
        "messages": st.history(chat_id, limit=200),
        "decisions": st.decisions(chat_id, limit=40),
        "deal": deal,
        "window_hours_left": window_left,
    }


@router.post("/dialogs/{chat_id}/state")
async def dialog_state(chat_id: str, request: Request, wa_session: str = Cookie(default="")):
    """Ручное вмешательство: забрать диалог у ИИ или вернуть обратно."""
    u = me(wa_session)
    body = await request.json()
    target = body.get("state")
    if target not in {"handed_off", "armed", "sleeping"}:
        raise HTTPException(400, "Недопустимое состояние")
    fields: dict[str, Any] = {"state": target}
    if target == "handed_off":
        fields["handed_off_at"] = time.time()
    st.upsert_dialog(chat_id, **fields)
    reason = ("Менеджер забрал диалог из панели" if target == "handed_off"
              else "Менеджер вернул диалог ИИ")
    st.add_decision(chat_id, "handoff" if target == "handed_off" else "silent", reason, [])
    acc.log(u["account_id"], u["email"], "dialog.state",
            {"chat_id": chat_id, "state": target})
    return {"ok": True, "state": target}


# ---------------- статистика ----------------

@router.get("/stats")
async def stats(days: int = 7, wa_session: str = Cookie(default="")):
    me(wa_session)
    since = time.time() - days * 86400

    def scalar(sql: str, params: tuple = ()) -> int:
        return st.q(sql, params)[0]["c"]

    total = scalar("SELECT COUNT(*) AS c FROM dialogs WHERE COALESCE(last_inbound_at,0) >= ?",
                   (since,))
    by_state = {r["state"]: r["c"] for r in st.q(
        "SELECT state, COUNT(*) AS c FROM dialogs GROUP BY state")}
    ai_msgs = scalar(
        "SELECT COUNT(*) AS c FROM messages WHERE author='ai' AND created_at >= ?", (since,))
    client_msgs = scalar(
        "SELECT COUNT(*) AS c FROM messages WHERE author='client' AND created_at >= ?", (since,))
    handoffs = scalar(
        "SELECT COUNT(*) AS c FROM decisions WHERE verdict='handoff' AND created_at >= ?",
        (since,))
    verdicts = {r["verdict"]: r["c"] for r in st.q(
        "SELECT verdict, COUNT(*) AS c FROM decisions WHERE created_at >= ?"
        " GROUP BY verdict", (since,))}
    silent_reasons = [dict(r) for r in st.q(
        "SELECT reason, COUNT(*) AS c FROM decisions WHERE verdict='silent'"
        " AND created_at >= ? GROUP BY reason ORDER BY c DESC LIMIT 6", (since,))]

    handled_by_ai = by_state.get("active", 0)
    return {
        "days": days,
        "dialogs": total,
        "by_state": by_state,
        "ai_messages": ai_msgs,
        "client_messages": client_msgs,
        "handoffs": handoffs,
        "verdicts": verdicts,
        "silent_reasons": silent_reasons,
        "handoff_rate": round(handoffs / total * 100, 1) if total else 0.0,
        "autonomy_rate": round(handled_by_ai / total * 100, 1) if total else 0.0,
        "pending_jobs": scalar("SELECT COUNT(*) AS c FROM jobs WHERE status='pending'"),
        "failed_jobs": scalar("SELECT COUNT(*) AS c FROM jobs WHERE status='failed'"),
    }


@router.get("/audit")
async def audit_list(wa_session: str = Cookie(default="")):
    u = me(wa_session)
    return {"items": acc.audit(u["account_id"])}
