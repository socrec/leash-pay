import os
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, update
from sqlmodel import Field, Session, SQLModel, create_engine


def now():
    return datetime.now(timezone.utc)


class Wallet(SQLModel, table=True):
    # ponytail: single demo user (id=1); add a user column + auth before real users.
    id: int | None = Field(default=None, primary_key=True)
    vault_id: str | None = None
    payer_email: str | None = None
    setup_token: str | None = None
    policy: dict | None = Field(default=None, sa_column=Column(JSON))


class Purchase(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=now)
    merchant: str
    items: list = Field(sa_column=Column(JSON))
    amount: float
    decision: str  # allow | approve | deny
    rule: str
    agent_reason: str
    status: str  # pending | charging | paid | failed | denied | rejected | refunding | refunded
    paypal_order_id: str | None = None
    capture_id: str | None = None


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    purchase_id: int | None = Field(default=None, index=True)
    type: str
    payload: dict | None = Field(default=None, sa_column=Column(JSON))
    at: datetime = Field(default_factory=now)


url = os.environ.get("DATABASE_URL", "sqlite:///leashpay.db")
engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})


def log(session: Session, purchase_id: int | None, type: str, payload: dict | None = None):
    session.add(Event(purchase_id=purchase_id, type=type, payload=payload))


def claim(session: Session, purchase_id: int, from_status: str, to_status: str) -> bool:
    """Atomically move a purchase between statuses; False if someone else got there first (double taps)."""
    result = session.exec(update(Purchase)
                          .where(Purchase.id == purchase_id, Purchase.status == from_status)
                          .values(status=to_status))
    session.commit()
    return result.rowcount == 1
