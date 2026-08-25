"""Слой amoCRM.

В mock-режиме сделка берётся из локального словаря, который правится
прямо из тестового чата — так проверяются правила без реальной CRM.
"""
from typing import Any, Optional

import httpx

from .config import settings

# Тестовая сделка. Тестовый чат меняет её на лету.
mock_deal: dict[str, Any] = {
    "id": 100500,
    "name": "Заявка с Instagram",
    "stage": "Первичный контакт",
    "price": 250000,
    "tags": [],
    "phone": "77011234567",
    "responsible": "Айгуль",
    "fields": {},
}

# Что ИИ записал обратно — для отображения в тестовом чате
mock_writeback: list[dict] = []


class AmoClient:
    def __init__(self) -> None:
        self.base = f"https://{settings.AMO_SUBDOMAIN}.amocrm.ru/api/v4"
        self.token = settings.AMO_ACCESS_TOKEN

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.request(method, f"{self.base}{path}", headers=self._headers, **kw)
            r.raise_for_status()
            return r.json() if r.content else {}

    async def find_lead_by_phone(self, phone: str) -> Optional[dict]:
        if settings.is_mock:
            return dict(mock_deal)
        data = await self._request("GET", f"/leads?query={phone}&with=contacts")
        leads = (data.get("_embedded") or {}).get("leads") or []
        return leads[0] if leads else None

    async def get_deal_context(self, phone: str) -> dict:
        """Нормализованный контекст сделки для промпта и правил."""
        if settings.is_mock:
            return dict(mock_deal)
        lead = await self.find_lead_by_phone(phone)
        if not lead:
            return {"stage": None, "tags": [], "fields": {}, "phone": phone}
        return {
            "id": lead.get("id"),
            "name": lead.get("name"),
            "stage": str(lead.get("status_id")),
            "price": lead.get("price"),
            "tags": [t.get("name") for t in (lead.get("_embedded") or {}).get("tags") or []],
            "phone": phone,
            "fields": {f.get("field_name"): (f.get("values") or [{}])[0].get("value")
                       for f in lead.get("custom_fields_values") or []},
        }

    async def add_note(self, lead_id: int, text: str) -> Any:
        if settings.is_mock:
            mock_writeback.append({"type": "note", "lead_id": lead_id, "text": text})
            return {"mock": True}
        return await self._request(
            "POST", f"/leads/{lead_id}/notes",
            json=[{"note_type": "common", "params": {"text": text}}],
        )

    async def add_tag(self, lead_id: int, tag: str) -> Any:
        if settings.is_mock:
            mock_writeback.append({"type": "tag", "lead_id": lead_id, "text": tag})
            if tag not in mock_deal["tags"]:
                mock_deal["tags"].append(tag)
            return {"mock": True}
        return await self._request(
            "PATCH", f"/leads/{lead_id}",
            json={"_embedded": {"tags": [{"name": tag}]}},
        )

    async def create_task(self, lead_id: int, text: str) -> Any:
        if settings.is_mock:
            mock_writeback.append({"type": "task", "lead_id": lead_id, "text": text})
            return {"mock": True}
        return await self._request(
            "POST", "/tasks",
            json=[{"entity_id": lead_id, "entity_type": "leads", "text": text}],
        )


amo = AmoClient()
