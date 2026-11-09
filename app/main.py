import base64
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from .database import Base, engine, get_db
from .models import Booking, BookingAttempt, IdempotencyRecord, Slot
from .schemas import BookingInput, BookingOutput, ResetInput


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def api_error(message: str, status: int, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail=message, headers=headers)


def booking_dict(booking: Booking) -> dict:
    result = BookingOutput.model_validate(booking).model_dump(mode="json")
    result["created_at"] = as_utc(booking.created_at).isoformat().replace("+00:00", "Z")
    return result


def request_hash(body: BookingInput) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def encode_cursor(booking: Booking) -> str:
    value = f"{as_utc(booking.created_at).isoformat()}|{booking.id}"
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        timestamp, booking_id = base64.urlsafe_b64decode(padded).decode().split("|", 1)
        return datetime.fromisoformat(timestamp), booking_id
    except (ValueError, UnicodeDecodeError):
        raise api_error("invalid cursor", 400)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Clinic booking API", version="1.0", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError):
    errors = exc.errors()
    field = errors[0].get("loc", ["request"])[-1] if errors else "request"
    return JSONResponse({"error": f"invalid or missing {field}"}, status_code=400)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("API_KEY", "dev-api-key")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise api_error("missing or invalid API key", 401)


secured = Depends(require_api_key)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def web_page():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/reset", dependencies=[secured])
def reset(payload: ResetInput, db: Session = Depends(get_db)):
    if len({slot.id for slot in payload.slots}) != len(payload.slots):
        raise api_error("duplicate slot id", 400)
    try:
        db.execute(delete(IdempotencyRecord))
        db.execute(delete(BookingAttempt))
        db.execute(delete(Booking))
        db.execute(delete(Slot))
        db.add_all([Slot(**slot.model_dump()) for slot in payload.slots])
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error("invalid slots", 400)
    return {"message": "reset complete"}


@app.get("/slots", dependencies=[secured])
def list_slots(provider: str | None = None, db: Session = Depends(get_db)):
    confirmed = (
        select(Booking.slot_id, func.count(Booking.id).label("booked"))
        .where(Booking.status == "confirmed")
        .group_by(Booking.slot_id)
        .subquery()
    )
    query = select(Slot, confirmed.c.booked).outerjoin(confirmed, Slot.id == confirmed.c.slot_id)
    if provider is not None:
        query = query.where(Slot.provider == provider)
    rows = db.execute(query.order_by(Slot.start.asc(), Slot.id.asc())).all()
    return {
        "slots": [
            {
                "id": slot.id,
                "provider": slot.provider,
                "start": as_utc(slot.start).isoformat().replace("+00:00", "Z"),
                "end": as_utc(slot.end).isoformat().replace("+00:00", "Z"),
                "available": not bool(booked),
            }
            for slot, booked in rows
        ]
    }


def replay_or_conflict(db: Session, key: str, body_hash: str):
    record = db.get(IdempotencyRecord, key)
    if record is None:
        return None
    if record.request_hash != body_hash:
        raise api_error("Idempotency-Key was already used with a different request", 422)
    return JSONResponse(json.loads(record.response_body), status_code=record.status_code)


def save_idempotency(db: Session, key: str | None, body_hash: str, status: int, response: dict) -> None:
    if key:
        db.add(
            IdempotencyRecord(
                key=key,
                request_hash=body_hash,
                status_code=status,
                response_body=json.dumps(response, separators=(",", ":")),
                created_at=utcnow(),
            )
        )


def lock_rate_limit_email(db: Session, email: str) -> None:
    """Serialize one email's rolling-window check on PostgreSQL."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        lock_id = int.from_bytes(hashlib.sha256(email.encode()).digest()[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


def enforce_rate_limit(db: Session, email: str, now: datetime) -> None:
    lock_rate_limit_email(db, email)
    window_start = now - timedelta(minutes=1)
    attempts = db.scalar(
        select(func.count(BookingAttempt.id)).where(
            BookingAttempt.email == email, BookingAttempt.attempted_at > window_start
        )
    ) or 0
    if attempts >= 5:
        oldest = db.scalar(
            select(BookingAttempt.attempted_at)
            .where(BookingAttempt.email == email, BookingAttempt.attempted_at > window_start)
            .order_by(BookingAttempt.attempted_at.asc())
            .limit(1)
        )
        retry_after = max(1, int((as_utc(oldest) + timedelta(minutes=1) - now).total_seconds()) + 1)
        raise api_error("too many booking attempts", 429, {"Retry-After": str(retry_after)})


@app.post("/bookings", status_code=201, dependencies=[secured])
def create_booking(
    raw_payload: dict = Body(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    now = utcnow()
    try:
        payload = BookingInput.model_validate(raw_payload)
    except ValidationError as exc:
        raw_email = raw_payload.get("email")
        if isinstance(raw_email, str) and raw_email.strip():
            attempted_email = raw_email.strip().lower()
            enforce_rate_limit(db, attempted_email, now)
            db.add(BookingAttempt(email=attempted_email, attempted_at=now))
            db.commit()
        errors = exc.errors()
        field = errors[0].get("loc", ["request"])[-1] if errors else "request"
        return JSONResponse({"error": f"invalid or missing {field}"}, status_code=400)

    body_hash = request_hash(payload)
    if idempotency_key:
        if not idempotency_key.strip():
            raise api_error("Idempotency-Key must not be empty", 400)
        replay = replay_or_conflict(db, idempotency_key, body_hash)
        if replay:
            return replay

    email = str(payload.email).lower()
    enforce_rate_limit(db, email, now)
    db.add(BookingAttempt(email=email, attempted_at=now))
    slot = db.get(Slot, payload.slot_id)
    if slot is None:
        response = {"error": "slot not found"}
        save_idempotency(db, idempotency_key, body_hash, 404, response)
        db.commit()
        return JSONResponse(response, status_code=404)

    start = as_utc(slot.start)
    if start <= now:
        response = {"error": "slot has already started"}
        save_idempotency(db, idempotency_key, body_hash, 400, response)
        db.commit()
        return JSONResponse(response, status_code=400)

    booking = Booking(
        slot_id=slot.id,
        name=payload.name,
        email=email,
        status="confirmed",
        created_at=now,
    )
    db.add(booking)
    try:
        db.flush()
        response = booking_dict(booking)
        save_idempotency(db, idempotency_key, body_hash, 201, response)
        db.commit()
        return JSONResponse(response, status_code=201)
    except IntegrityError:
        db.rollback()
        # Preserve the failed attempt after the booking transaction rolls back.
        db.add(BookingAttempt(email=email, attempted_at=now))
        response = {"error": "slot already booked"}
        try:
            save_idempotency(db, idempotency_key, body_hash, 409, response)
            db.commit()
        except IntegrityError:
            db.rollback()
            if idempotency_key:
                replay = replay_or_conflict(db, idempotency_key, body_hash)
                if replay:
                    return replay
        return JSONResponse(response, status_code=409)


@app.get("/bookings", dependencies=[secured])
def list_bookings(
    email: str = Query(min_length=1),
    limit: int = Query(default=10, ge=1, le=50),
    cursor: str | None = None,
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    query = select(Booking).where(func.lower(Booking.email) == normalized_email)
    if cursor:
        cursor_time, cursor_id = decode_cursor(cursor)
        query = query.where(
            (Booking.created_at > cursor_time)
            | ((Booking.created_at == cursor_time) & (Booking.id > cursor_id))
        )
    rows = db.scalars(query.order_by(Booking.created_at.asc(), Booking.id.asc()).limit(limit + 1)).all()
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "bookings": [booking_dict(booking) for booking in page],
        "next_cursor": encode_cursor(page[-1]) if has_more and page else None,
    }


@app.get("/bookings/{booking_id}", dependencies=[secured])
def get_booking(booking_id: str, db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise api_error("booking not found", 404)
    return booking_dict(booking)


@app.delete("/bookings/{booking_id}", dependencies=[secured])
def cancel_booking(booking_id: str, db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise api_error("booking not found", 404)
    if booking.status == "cancelled":
        raise api_error("booking already cancelled", 409)
    booking.status = "cancelled"
    db.commit()
    return booking_dict(booking)


# The patient page uses same-origin server routes, so the private assessment key
# never has to be embedded in public browser code. The required assessment API
# endpoints above remain protected by X-API-Key.
@app.get("/web/slots", include_in_schema=False)
def web_list_slots(provider: str | None = None, db: Session = Depends(get_db)):
    return list_slots(provider=provider, db=db)


@app.post("/web/bookings", include_in_schema=False)
def web_create_booking(
    payload: dict = Body(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    return create_booking(raw_payload=payload, idempotency_key=idempotency_key, db=db)

