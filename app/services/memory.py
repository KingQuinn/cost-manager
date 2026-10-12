"""3-layer conversation memory for the WhatsApp agent (architecture.md §7.7).

Layer 1 — cache (app/services/cache.py, wired in app/db/instrumented.py)
    Short-TTL, in-process copies of hot reads that rarely change: member by
    phone, family members, categories, and this member's long-term memories.
    Dropped as soon as the app writes to them. Not handled in this module —
    every read below that goes through the store benefits automatically.

Layer 2 — conversation state (`conversation_turns` + `member_state`)
    Recent turns: the last N messages between one member and the bot, so the
    router can resolve "yes", "the second one", "what about last week?".
    Slots: what the bot is waiting on right now, with a TTL:
      awaiting_description — we asked them to type out an unreadable receipt
      pending_action       — we proposed a data change and need a yes/no
    Open clarifications are also state, but they already live in the
    `clarifications` table, so they are read from there, not duplicated here.

Layer 3 — long-term memory (`member_memories`)
    Durable facts the member told us ("our grocery budget is 60k a week",
    "Mama Put is Dining") plus one rolling summary of older conversation.
    When stored turns grow past MEMORY_COMPACT_AFTER, the oldest are folded
    into that summary and deleted, so prompts stay small and cheap.

Spending data itself is NOT memory: receipts/line items are the source of
truth and totals are computed by the database on every question.

Layers 2 and 3 live in the DataStore, so the webhook handler stays stateless
(architecture.md §4 availability requirement).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from uuid import UUID

from app.config import get_settings
from app.db import DataStore
from app.models import ConversationTurn, MemberMemory

logger = logging.getLogger(__name__)

MAX_FACTS = 40
MAX_TURN_CHARS = 1500  # long pastes get clipped in stored turns


class ConversationMemory:
    def __init__(self, store: DataStore) -> None:
        self.store = store
        s = get_settings()
        self.recent_turns = s.memory_recent_turns
        self.compact_after = s.memory_compact_after
        self.state_ttl = timedelta(minutes=s.memory_state_ttl_minutes)

    # ---------------- layer 2: recent turns ----------------
    def record(self, member_id: UUID, role: str, content: str, intent: Optional[str] = None) -> None:
        try:
            self.store.add_conversation_turn(
                ConversationTurn(
                    member_id=member_id, role=role, content=(content or "")[:MAX_TURN_CHARS], intent=intent
                )
            )
        except Exception as e:  # memory is helpful, never critical
            logger.warning("Could not record conversation turn: %s", e)

    def recent(self, member_id: UUID) -> list[ConversationTurn]:
        try:
            return self.store.list_conversation_turns(member_id, limit=self.recent_turns)
        except Exception as e:
            logger.warning("Could not load conversation turns: %s", e)
            return []

    # ---------------- layer 2: state slots ----------------
    def state(self, member_id: UUID) -> dict[str, Any]:
        """Live (non-expired) state slots: {name: value}."""
        try:
            raw = self.store.get_member_state(member_id)
        except Exception as e:
            logger.warning("Could not load member state: %s", e)
            return {}
        now = datetime.utcnow()
        live: dict[str, Any] = {}
        for key, slot in (raw or {}).items():
            if not isinstance(slot, dict) or "set_at" not in slot:
                continue
            try:
                set_at = datetime.fromisoformat(slot["set_at"])
            except (TypeError, ValueError):
                continue
            if now - set_at <= self.state_ttl:
                live[key] = slot.get("value")
        return live

    def set_state(self, member_id: UUID, key: str, value: Any) -> None:
        try:
            raw = self.store.get_member_state(member_id) or {}
            raw[key] = {"value": value, "set_at": datetime.utcnow().isoformat()}
            self.store.save_member_state(member_id, raw)
        except Exception as e:
            logger.warning("Could not save member state %s: %s", key, e)

    def clear_state(self, member_id: UUID, *keys: str) -> None:
        try:
            raw = self.store.get_member_state(member_id) or {}
            changed = False
            for k in keys:
                if k in raw:
                    raw.pop(k)
                    changed = True
            if changed:
                self.store.save_member_state(member_id, raw)
        except Exception as e:
            logger.warning("Could not clear member state: %s", e)

    # ---------------- layer 3: long-term memory ----------------
    def long_term(self, member_id: UUID) -> list[MemberMemory]:
        try:
            return self.store.list_member_memories(member_id)
        except Exception as e:
            logger.warning("Could not load long-term memory: %s", e)
            return []

    def remember(self, member_id: UUID, facts: list[str]) -> list[str]:
        """Store new durable facts (deduped, case-insensitive). Returns what was added."""
        clean = [f.strip()[:300] for f in facts or [] if isinstance(f, str) and f.strip()]
        if not clean:
            return []
        existing = self.long_term(member_id)
        seen = {m.content.strip().lower() for m in existing}
        added: list[str] = []
        try:
            for fact in clean:
                if fact.lower() in seen:
                    continue
                self.store.add_member_memory(MemberMemory(member_id=member_id, kind="fact", content=fact))
                seen.add(fact.lower())
                added.append(fact)
            facts_now = [m for m in self.store.list_member_memories(member_id) if m.kind == "fact"]
            for old in facts_now[: max(0, len(facts_now) - MAX_FACTS)]:
                self.store.delete_member_memory(old.id)
        except Exception as e:
            logger.warning("Could not save long-term memory: %s", e)
        return added

    def compact_if_needed(
        self, member_id: UUID, summarize: Callable[[Optional[str], list[ConversationTurn]], str]
    ) -> bool:
        """Fold the oldest turns into the rolling summary once stored turns grow too long.

        `summarize(previous_summary, old_turns) -> new_summary` is an LLM call;
        if it fails, nothing is deleted (we'd rather keep raw turns than lose them).
        """
        try:
            turns = self.store.list_conversation_turns(member_id)
        except Exception:
            return False
        if len(turns) <= self.compact_after:
            return False
        old = turns[: len(turns) - self.recent_turns]
        memories = self.long_term(member_id)
        prev = next((m for m in reversed(memories) if m.kind == "summary"), None)
        try:
            new_summary = (summarize(prev.content if prev else None, old) or "").strip()
        except Exception as e:
            logger.warning("Conversation summary failed; keeping raw turns: %s", e)
            return False
        if not new_summary:
            return False
        try:
            self.store.add_member_memory(
                MemberMemory(member_id=member_id, kind="summary", content=new_summary[:2000])
            )
            for m in memories:
                if m.kind == "summary":
                    self.store.delete_member_memory(m.id)
            self.store.delete_conversation_turns([t.id for t in old])
        except Exception as e:
            logger.warning("Could not store conversation summary: %s", e)
            return False
        return True
