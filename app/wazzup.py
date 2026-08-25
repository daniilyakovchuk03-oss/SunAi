"""Клиент Wazzup API v3.

В mock-режиме наружу ничего не уходит: ответы складываются в локальный буфер,
который читает тестовый чат. Так логику можно гонять без реального номера.
"""
import time
from typing import Any, Optional

import httpx

from .config import settings

# Буфер исходящих для тестового режима
mock_outbox: list[dict] = []


class WazzupError(RuntimeError):
    pass


class WazzupClient:
    def __init__(self) -> None:
        self.base = settings.WAZZUP_BASE_URL
        self.key = settings.WAZZUP_API_KEY

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        if settings.is_mock:
            return {"mock": True}
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.request(method, f"{self.base}{path}", headers=self._headers, **kw)
            if r.status_code >= 400:
                raise WazzupError(f"{r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}

    async def send_text(self, chat_id: str, text: str,
                        chat_type: str = "whatsapp",
                        channel_id: Optional[str] = None) -> dict:
        """Отправка сообщения. crmUserId помечает автора как ИИ."""
        body = {
            "channelId": channel_id or settings.WAZZUP_CHANNEL_ID,
            "chatId": chat_id,
            "chatType": chat_type,
            "text": text,
            "crmUserId": settings.AI_CRM_USER_ID,
        }
        if settings.is_mock:
            entry = {**body, "sentAt": time.time(), "id": f"mock-{len(mock_outbox) + 1}"}
            mock_outbox.append(entry)
            return entry
        return await self._request("POST", "/message", json=body)

    async def channels(self) -> Any:
        return await self._request("GET", "/channels")

    async def users(self) -> Any:
        return await self._request("GET", "/users")

    async def get_webhooks(self) -> Any:
        return await self._request("GET", "/webhooks")

    async def subscribe_webhooks(self, url: str) -> Any:
        """Подписка на вебхуки. Wazzup сразу шлёт тестовый POST {"test": true},
        на который надо ответить 200 — это делает наш обработчик."""
        body = {
            "webhooksUri": url,
            "subscriptions": {
                "messagesAndStatuses": True,
                "contactsAndDealsCreation": True,
            },
        }
        return await self._request("PATCH", "/webhooks", json=body)


wazzup = WazzupClient()


def parse_webhook(payload: dict) -> list[dict]:
    """Приводит вебхук Wazzup к плоскому списку событий.

    Вебхук может содержать messages и statuses одновременно,
    а также createContact / createDeal по отдельности.
    """
    events: list[dict] = []
    for m in payload.get("messages") or []:
        events.append({
            "kind": "message",
            "chat_id": m.get("chatId"),
            "chat_type": m.get("chatType", "whatsapp"),
            "channel_id": m.get("channelId"),
            "message_id": m.get("messageId") or m.get("id"),
            "is_echo": bool(m.get("isEcho")),
            "status": m.get("status"),
            "text": m.get("text") or "",
            "crm_user_id": m.get("crmUserId"),
            "author_name": m.get("authorName"),
            "contact": m.get("contact") or {},
            "raw": m,
        })
    for s in payload.get("statuses") or []:
        events.append({"kind": "status", "message_id": s.get("messageId"),
                       "status": s.get("status"), "raw": s})
    if payload.get("createContact"):
        events.append({"kind": "create_contact", "raw": payload["createContact"]})
    if payload.get("createDeal"):
        events.append({"kind": "create_deal", "raw": payload["createDeal"]})
    return events


def classify_author(event: dict) -> str:
    """Кто написал: клиент, наш ИИ или менеджер руками.

    Это ядро защиты от коллизий. Исходящее с нашим crmUserId — это ИИ.
    Любое другое исходящее — вмешался человек, ИИ должен замолчать.
    """
    if event.get("is_echo") or event.get("status") in {"sent", "delivered", "read"}:
        if event.get("crm_user_id") == settings.AI_CRM_USER_ID:
            return "ai"
        return "manager"
    return "client"
