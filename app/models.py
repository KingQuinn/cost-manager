from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime as datetime_cls
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Gemini extraction output — exact schema from architecture.md §9.2
# ---------------------------------------------------------------------------

class ExtractedLineItem(BaseModel):
    name: str
    quantity: Decimal = Decimal("1")
    unit_price: Optional[Decimal] = None
    total_price: Decimal
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    vendor: Optional[str] = None
    date: Optional[date_cls] = None
    currency: str = "NGN"
    total: Optional[Decimal] = None
    line_items: list[ExtractedLineItem] = []
    overall_confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    extraction_notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Domain entities — mirror the Supabase tables in architecture.md §8.2
# ---------------------------------------------------------------------------

class Family(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    created_at: datetime_cls = Field(default_factory=datetime_cls.utcnow)


class Member(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    family_id: UUID
    name: str
    phone_number: str
    created_at: datetime_cls = Field(default_factory=datetime_cls.utcnow)


class Category(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    keywords: list[str] = []


DEFAULT_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Groceries", ["rice","garri","beans","tomato","pepper","onion","yam","shoprite","market"]),
    ("Transport", ["uber","bolt","keke","fuel","petrol","diesel","fare"]),
    ("Household", ["detergent","tissue","cleaning","bulb","omo","soap"]),
    ("Utilities", ["electricity","nepa","data","airtime","internet","dstv"]),
    ("Dining", ["restaurant","food","suya","shawarma","amala","buka"]),
    ("Personal", ["salon","barber","clothing","shoes","wig"]),
    ("Other", []),
]


class ReceiptStatus(str):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"


class Receipt(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    member_id: UUID
    vendor: Optional[str] = None
    purchase_date: Optional[date_cls] = None
    total: Optional[Decimal] = None
    image_url: Optional[str] = None
    status: str = ReceiptStatus.PENDING
    raw_extraction: dict | list | None = None
    created_at: datetime_cls = Field(default_factory=datetime_cls.utcnow)


class LineItem(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    receipt_id: UUID
    name: str
    quantity: Decimal = Decimal("1")
    price: Decimal
    category_id: Optional[UUID] = None
    confidence: Optional[float] = None
    needs_review: bool = False


class Clarification(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    receipt_id: UUID
    line_item_id: Optional[UUID] = None
    question: str
    resolved: bool = False
    answer: Optional[str] = None
    created_at: datetime_cls = Field(default_factory=datetime_cls.utcnow)


class WeeklySummary(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    family_id: UUID
    week_start: date_cls
    week_end: date_cls
    summary_text: str
    total_spend: Decimal = Decimal("0")
    created_at: datetime_cls = Field(default_factory=datetime_cls.utcnow)


# ---------------------------------------------------------------------------
# Message / webhook DTOs
# ---------------------------------------------------------------------------

class WhatsAppInboundMessage(BaseModel):
    """Normalized inbound message, regardless of source (real webhook or test)."""
    from_phone: str
    message_id: str
    text: Optional[str] = None
    image_id: Optional[str] = None
    image_bytes: Optional[bytes] = None
    timestamp: datetime_cls = Field(default_factory=datetime_cls.utcnow)


class WhatsAppStatusUpdate(BaseModel):
    """Normalized delivery-status event for a message we sent (sent/delivered/read/failed).

    Meta delivers these on the same webhook as inbound messages, under
    `value.statuses` rather than `value.messages` — see architecture.md §9.1.
    """
    message_id: str
    recipient_phone: str
    status: str
    timestamp: datetime_cls = Field(default_factory=datetime_cls.utcnow)
    error: Optional[str] = None
