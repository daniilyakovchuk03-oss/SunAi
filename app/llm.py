"""Генерация ответа.

provider=stub — детерминированная заглушка без сети. Нужна, чтобы обкатывать
логику правил и состояний, не тратя токены и не ожидая ответа модели.
"""
import re
from typing import Any

import httpx

from .config import settings

SYSTEM_TEMPLATE = """Ты — сотрудник отдела продаж компании. Общаешься с клиентом в WhatsApp.

Контекст сделки:
- Этап: {stage}
- Сумма: {price}
- Ответственный менеджер: {responsible}
- Теги: {tags}

Правила общения:
- Пиши коротко, 1–3 предложения. Это мессенджер, не письмо.
- Обращение на «вы», без канцелярита и без восклицательных знаков через слово.
- Не обещай скидок, точных сроков и гарантий — это решает менеджер.
- Не выдумывай факты о продукте. Чего не знаешь — предложи уточнить у менеджера.
- Одно сообщение — одна мысль. Не задавай больше одного вопроса за раз.

Твоя задача: {goal}
"""

STUB_REPLIES = [
    "Здравствуйте. Подскажите, для какой задачи подбираете?",
    "Поняла вас. Уточните, пожалуйста, в какие сроки планируете начать?",
    "Спасибо. Передам менеджеру, он свяжется с вами и уточнит детали.",
    "Да, такой вариант у нас есть. Вам удобнее созвониться сегодня или завтра?",
]


def _stub(history: list[dict], deal: dict) -> str:
    idx = len([m for m in history if m["author"] == "ai"]) % len(STUB_REPLIES)
    return STUB_REPLIES[idx]


async def _anthropic(history: list[dict], deal: dict, goal: str, kb: str,
                     style: dict | None = None) -> str:
    style = style or {}
    system = SYSTEM_TEMPLATE.format(
        stage=deal.get("stage") or "не указан",
        price=deal.get("price") or "не указана",
        responsible=deal.get("responsible") or "не назначен",
        tags=", ".join(deal.get("tags") or []) or "нет",
        goal=goal,
    )
    if style.get("tone"):
        system += f"\n\nТон: {style['tone']}"
    if style.get("forbidden"):
        system += f"\n\nЗапрещено: {style['forbidden']}"
    if style.get("max_sentences"):
        system += f"\n\nМаксимум {style['max_sentences']} предложения в ответе."
    if kb:
        system += f"\n\nБаза знаний (отвечай только по ней):\n{kb}"

    messages: list[dict[str, Any]] = []
    for m in history[-20:]:
        role = "assistant" if m["author"] == "ai" else "user"
        prefix = "[менеджер] " if m["author"] == "manager" else ""
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + prefix + m["text"]
        else:
            messages.append({"role": role, "content": prefix + m["text"]})
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "Здравствуйте"})

    async with httpx.AsyncClient(timeout=45) as cli:
        r = await cli.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": settings.LLM_MODEL,
                "max_tokens": 300,
                "system": system,
                "messages": messages,
            },
        )
        r.raise_for_status()
        data = r.json()
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


async def generate(history: list[dict], deal: dict,
                   goal: str = "квалифицировать клиента и довести до записи на встречу",
                   kb: str = "", style: dict | None = None) -> str:
    if settings.LLM_PROVIDER == "stub" or not settings.ANTHROPIC_API_KEY:
        return _stub(history, deal)
    try:
        text = await _anthropic(history, deal, goal, kb, style or {})
        return re.sub(r"\n{3,}", "\n\n", text) or _stub(history, deal)
    except Exception:
        return _stub(history, deal)
