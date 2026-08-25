"""Движок правил.

Главный принцип: каждое решение возвращается вместе с трассировкой проверок.
Никаких молчаливых отказов — всегда видно, какое правило сработало.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import yaml

from .config import settings

_cache: dict[str, Any] = {"mtime": 0.0, "data": {}}

# Провайдер правил. Админка подставляет сюда функцию чтения из базы,
# и тогда YAML используется только как начальное значение при первом запуске.
_provider: Optional[Callable[[], dict]] = None


def set_provider(fn: Optional[Callable[[], dict]]) -> None:
    global _provider
    _provider = fn


def load_yaml(force: bool = False) -> dict:
    """Читает rules.yaml с диска. Перечитывает файл, если он изменился."""
    path = Path(settings.RULES_PATH)
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    if force or mtime != _cache["mtime"]:
        with path.open(encoding="utf-8") as f:
            _cache["data"] = yaml.safe_load(f) or {}
        _cache["mtime"] = mtime
    return _cache["data"]


def load_rules(force: bool = False) -> dict:
    """Актуальные правила: из базы, если админка подключена, иначе из файла."""
    if _provider is not None:
        data = _provider()
        if data:
            return data
    return load_yaml(force)

PROFANITY = {"хуй", "пизд", "бляд", "ебан", "сука", "мудак", "долбо"}
PRICE_HAGGLE = {"дорого", "скидк", "дешевле", "торг", "уступ", "снизьте", "подешевле"}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class Decision:
    verdict: str                       # reply | silent | handoff | queue
    reason: str
    checks: list[Check] = field(default_factory=list)

    @property
    def should_reply(self) -> bool:
        return self.verdict == "reply"

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "checks": [c.as_dict() for c in self.checks],
        }


def _norm(values: Any) -> list[str]:
    if not values:
        return []
    return [str(v).strip().lower() for v in values]


def _within_schedule(rules: dict) -> tuple[bool, str]:
    sched = rules.get("schedule") or {}
    tz = ZoneInfo(sched.get("timezone", settings.TZ))
    now = datetime.now(tz)
    workdays = sched.get("workdays", [1, 2, 3, 4, 5])
    if now.isoweekday() not in workdays:
        return False, f"нерабочий день ({now.strftime('%A')})"
    start = dtime.fromisoformat(sched.get("start", "09:00"))
    end = dtime.fromisoformat(sched.get("end", "21:00"))
    if not (start <= now.time() <= end):
        return False, f"вне часов работы ({now.strftime('%H:%M')})"
    return True, now.strftime("%H:%M")


def evaluate(*, dialog: dict, deal: dict, message: str,
             history: list[dict] | None = None) -> Decision:
    """Основная точка входа.

    dialog — строка из таблицы dialogs (dict)
    deal   — данные сделки из amoCRM: stage, tags, fields, phone
    """
    rules = load_rules()
    checks: list[Check] = []
    history = history or []

    def stop(verdict: str, reason: str) -> Decision:
        return Decision(verdict=verdict, reason=reason, checks=checks)

    # --- 0. Глобальный выключатель ---
    if not rules.get("enabled", True):
        checks.append(Check("Система включена", False, "enabled: false в rules.yaml"))
        return stop("silent", "ИИ выключен глобально")
    checks.append(Check("Система включена", True))

    tags = _norm(deal.get("tags"))
    stop_cfg = rules.get("stop_list") or {}
    manual = rules.get("manual_switch") or {}
    limits = rules.get("limits") or {}
    handoff_cfg = rules.get("handoff") or {}
    text_low = (message or "").lower()

    # --- 1. Диалог уже передан человеку ---
    if dialog.get("state") == "handed_off":
        checks.append(Check("Не передан человеку", False, "менеджер ведёт диалог"))
        return stop("silent", "Диалог у менеджера")
    checks.append(Check("Не передан человеку", True))

    # --- 2. Стоп-лист. Проверяется раньше всех разрешений ---
    banned = set(_norm(stop_cfg.get("tags")))
    hit = banned & set(tags)
    if hit:
        checks.append(Check("Стоп-лист", False, f"тег: {', '.join(hit)}"))
        return stop("silent", f"Клиент в стоп-листе по тегу «{list(hit)[0]}»")

    phone = str(deal.get("phone") or dialog.get("phone") or "")
    if phone and phone in [str(p) for p in (stop_cfg.get("phones") or [])]:
        checks.append(Check("Стоп-лист", False, f"номер {phone}"))
        return stop("silent", "Номер в стоп-листе")

    for flag in stop_cfg.get("field_flags") or []:
        val = (deal.get("fields") or {}).get(flag.get("field"))
        if flag.get("when_truthy") and val:
            checks.append(Check("Стоп-лист", False, f"поле «{flag['field']}» заполнено"))
            return stop("silent", f"Поле «{flag['field']}» запрещает бота")
    checks.append(Check("Стоп-лист", True))

    # --- 3. Ручной тумблер перекрывает правило по этапам ---
    off_tag = str(manual.get("off_tag", "")).lower()
    on_tag = str(manual.get("on_tag", "")).lower()
    manual_on = False
    if off_tag and off_tag in tags:
        checks.append(Check("Ручной тумблер", False, f"тег «{off_tag}»"))
        return stop("silent", "Менеджер выключил ИИ тегом")
    if on_tag and on_tag in tags:
        manual_on = True
        checks.append(Check("Ручной тумблер", True, f"включён тегом «{on_tag}»"))

    # --- 4. Этап воронки ---
    stages = rules.get("stages") or {}
    stage = str(deal.get("stage") or "").strip().lower()
    values = _norm(stages.get("values"))
    if manual_on:
        checks.append(Check("Этап воронки", True, "перекрыт ручным тумблером"))
    elif stages.get("mode") == "blocklist":
        if stage in values:
            checks.append(Check("Этап воронки", False, f"«{deal.get('stage')}» в блок-листе"))
            return stop("silent", f"Этап «{deal.get('stage')}» запрещён")
        checks.append(Check("Этап воронки", True, deal.get("stage") or "—"))
    else:
        if stage not in values:
            checks.append(Check("Этап воронки", False, f"«{deal.get('stage')}» не в списке"))
            return stop("silent", f"Этап «{deal.get('stage')}» вне зоны работы ИИ")
        checks.append(Check("Этап воронки", True, deal.get("stage") or "—"))

    # --- 5. Окно WhatsApp ---
    window_hours = float(limits.get("window_hours", 24))
    last_in = dialog.get("last_inbound_at") or 0
    if last_in:
        age_h = (time.time() - last_in) / 3600
        if age_h > window_hours:
            checks.append(Check("Окно 24 ч", False, f"истекло {age_h:.1f} ч назад"))
            return stop("silent", "Окно закрыто — нужен платный шаблон")
        checks.append(Check("Окно 24 ч", True, f"осталось {window_hours - age_h:.1f} ч"))
    else:
        checks.append(Check("Окно 24 ч", True, "открыто входящим"))

    # --- 6. Передача человеку по содержанию сообщения ---
    for kw in _norm(handoff_cfg.get("keywords")):
        if kw in text_low:
            checks.append(Check("Триггеры передачи", False, f"слово «{kw}»"))
            return stop("handoff", f"Клиент просит человека («{kw}»)")

    if handoff_cfg.get("on_profanity") and any(p in text_low for p in PROFANITY):
        checks.append(Check("Триггеры передачи", False, "нецензурная лексика"))
        return stop("handoff", "Клиент раздражён — нужен менеджер")

    if handoff_cfg.get("on_price_negotiation") and any(p in text_low for p in PRICE_HAGGLE):
        checks.append(Check("Триггеры передачи", False, "торг по цене"))
        return stop("handoff", "Клиент торгуется — решение за менеджером")

    repeat_n = int(handoff_cfg.get("on_repeat_question") or 0)
    if repeat_n:
        client_msgs = [m["text"].strip().lower() for m in history if m["author"] == "client"]
        if len(client_msgs) >= repeat_n and len(set(client_msgs[-repeat_n:])) == 1:
            checks.append(Check("Триггеры передачи", False, f"вопрос повторён {repeat_n} раза"))
            return stop("handoff", "Клиент повторяет вопрос — ИИ не справился")
    checks.append(Check("Триггеры передачи", True, "не сработали"))

    # --- 7. Лимиты ---
    max_msgs = int(limits.get("max_ai_messages_per_dialog", 20))
    if (dialog.get("ai_message_count") or 0) >= max_msgs:
        checks.append(Check("Лимит реплик", False, f"{dialog['ai_message_count']}/{max_msgs}"))
        return stop("handoff", "Исчерпан лимит реплик ИИ в диалоге")
    checks.append(Check("Лимит реплик", True,
                        f"{dialog.get('ai_message_count') or 0}/{max_msgs}"))

    min_gap = float(limits.get("min_seconds_between_replies", 5))
    last_out = dialog.get("last_outbound_at") or 0
    if last_out and (time.time() - last_out) < min_gap:
        checks.append(Check("Анти-флуд", False, f"прошло меньше {min_gap} с"))
        return stop("silent", "Слишком частые ответы")
    checks.append(Check("Анти-флуд", True))

    # --- 8. Расписание ---
    ok, detail = _within_schedule(rules)
    if not ok:
        mode = (rules.get("schedule") or {}).get("outside_hours", "queue")
        checks.append(Check("Часы работы", False, detail))
        if mode == "queue":
            return stop("queue", f"Ответ отложен до начала рабочего дня ({detail})")
        return stop("silent", f"Вне часов работы ({detail})")
    checks.append(Check("Часы работы", True, detail))

    return Decision(verdict="reply", reason="Все проверки пройдены", checks=checks)
