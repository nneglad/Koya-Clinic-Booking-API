from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.exceptions import RequestValidationError
from fastapi.security import APIKeyHeader
from sqlalchemy import create_engine, Column, String, DateTime, Boolean, ForeignKey, Text, Integer
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic import BaseModel, EmailStr
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import os
import uuid
import hashlib
import json
import time
import math

app = FastAPI(
    title="Clinic Booking API",
    description="An API for managing clinic appointment bookings",
    version="1.0.0"
)

@app.get("/")
def home():
    file_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "static",
        "index.html"
    )
    return FileResponse(file_path)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=exc.headers
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError
):
    return JSONResponse(
        status_code=400,
        content={"error": "Invalid or missing request data"}
    )
load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./clinic.db"
)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1
    )

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True
    )

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

class Slot(Base):
    __tablename__ = "slots"

    id = Column(String, primary_key=True, index=True)
    provider = Column(String, nullable=False)
    start = Column(DateTime(timezone=True), nullable=False)
    end = Column(DateTime(timezone=True), nullable=False)

class Booking(Base):
    __tablename__ = "bookings"

    id = Column(String, primary_key=True, index=True)
    slot_id = Column(String, ForeignKey("slots.id"), nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="confirmed")
    created_at = Column(DateTime(timezone=True), nullable=False)

class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    key = Column(String, primary_key=True)
    request_hash = Column(String, nullable=False)
    response_body = Column(Text, nullable=False)
    response_status = Column(Integer, nullable=False)

class BookingAttempt(Base):
    __tablename__ = "booking_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, nullable=False, index=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SlotInput(BaseModel):
    id: str
    provider: str
    start: datetime
    end: datetime


class ResetRequest(BaseModel):
    slots: list[SlotInput]


class BookingCreate(BaseModel):
    slot_id: str
    name: str
    email: EmailStr


api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False
)


def verify_api_key(api_key: str = Depends(api_key_header)):
    expected_key = os.getenv("API_KEY")

    if not expected_key or api_key != expected_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key"
        )

    return api_key

class SlotResponse(BaseModel):
    id: str
    provider: str
    start: datetime
    end: datetime
    available: bool

class ResetResponse(BaseModel):
    slots: list[SlotResponse]

def slot_to_response(slot: Slot, db: Session) -> dict:
    booking = db.query(Booking).filter(
        Booking.slot_id == slot.id,
        Booking.status == "confirmed"
    ).first()

    return {
        "id": slot.id,
        "provider": slot.provider,
        "start": slot.start,
        "end": slot.end,
        "available": booking is None
    }

@app.post("/reset")
def reset_slots(
    request: ResetRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    db.query(Booking).delete()
    db.query(Slot).delete()

    for slot_data in request.slots:
        new_slot = Slot(
            id=slot_data.id,
            provider=slot_data.provider,
            start=slot_data.start,
            end=slot_data.end
        )
        db.add(new_slot)

    db.commit()

    return {"message": "Slots reset successfully"}

@app.get("/slots")
def get_slots(
    provider: str = None,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    query = db.query(Slot)

    if provider:
        query = query.filter(Slot.provider == provider)

    slots = query.order_by(Slot.start.asc()).all()

    return {
        "slots": [
            slot_to_response(slot, db)
            for slot in slots
        ]
    }



@app.post("/bookings", status_code=201)
def create_booking(
    booking_data: BookingCreate,
    idempotency_key: str = Header(
        default=None,
        alias="Idempotency-Key"
    ),
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    # Check the patient's information.
    name = booking_data.name.strip()
    email = str(booking_data.email).strip().lower()

    # Check how many booking attempts this email has made
    # during the last 60 seconds.
    now = datetime.now(timezone.utc)
    one_minute_ago = now - timedelta(seconds=60)

    recent_attempts = db.query(BookingAttempt).filter(
        BookingAttempt.email == email,
        BookingAttempt.attempted_at >= one_minute_ago
    ).order_by(BookingAttempt.attempted_at.asc()).all()

    # Allow no more than five attempts per minute.
    if len(recent_attempts) >= 5:
        oldest_attempt = recent_attempts[0].attempted_at

        if oldest_attempt.tzinfo is None:
            oldest_attempt = oldest_attempt.replace(tzinfo=timezone.utc)

        retry_after = max(
            1,
            math.ceil(60 - (now - oldest_attempt).total_seconds())
        )

        raise HTTPException(
            status_code=429,
            detail="Too many booking attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)}
        )

    # Record this attempt, including attempts that later fail.
    db.add(
        BookingAttempt(
            email=email,
            attempted_at=now
        )
    )
    db.commit()

    if not name or not email:
        raise HTTPException(
            status_code=400,
            detail="Name and email are required"
        )

    # Prepare a consistent representation of the request.
    request_data = {
        "slot_id": booking_data.slot_id,
        "name": name,
        "email": email
    }

    request_hash = hashlib.sha256(
        json.dumps(request_data, sort_keys=True).encode()
    ).hexdigest()

    # Check whether this idempotency key was used before.
    if idempotency_key:
        previous_request = db.query(IdempotencyRecord).filter(
            IdempotencyRecord.key == idempotency_key
        ).first()

        if previous_request:
            if previous_request.request_hash != request_hash:
                raise HTTPException(
                    status_code=422,
                    detail="Idempotency key was used with a different request"
                )

            return JSONResponse(
                status_code=previous_request.response_status,
                content=json.loads(previous_request.response_body)
            )

    # Check whether the requested slot exists.
    slot = db.query(Slot).filter(
        Slot.id == booking_data.slot_id
    ).first()

    if not slot:
        raise HTTPException(
            status_code=404,
            detail="Appointment slot not found"
        )

    # Prevent two confirmed bookings for the same slot.
    existing_booking = db.query(Booking).filter(
        Booking.slot_id == slot.id,
        Booking.status == "confirmed"
    ).first()

    if existing_booking:
        raise HTTPException(
            status_code=409,
            detail="This appointment slot has already been booked"
        )

    # Check whether the appointment time has already started.
    slot_start = slot.start

    if slot_start.tzinfo is None:
        slot_start = slot_start.replace(tzinfo=timezone.utc)

    if slot_start <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail="This appointment slot has already started"
        )

    # Create the booking record.
    new_booking = Booking(
        id=str(uuid.uuid4()),
        slot_id=slot.id,
        name=name,
        email=email,
        status="confirmed",
        created_at=datetime.now(timezone.utc)
    )

    response_data = {
        "id": new_booking.id,
        "slot_id": new_booking.slot_id,
        "name": new_booking.name,
        "email": new_booking.email,
        "status": new_booking.status,
        "created_at": new_booking.created_at.isoformat()
    }

    db.add(new_booking)

    # Save the original response when an idempotency key is supplied.
    if idempotency_key:
        record = IdempotencyRecord(
            key=idempotency_key,
            request_hash=request_hash,
            response_body=json.dumps(response_data),
            response_status=201
        )
        db.add(record)

    db.commit()

    return response_data


@app.get("/bookings/{booking_id}")
def get_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    # Find the booking using its unique ID.
    booking = db.query(Booking).filter(
        Booking.id == booking_id
    ).first()

    # Return an error if the booking does not exist.
    if not booking:
        raise HTTPException(
            status_code=404,
            detail="Booking not found"
        )

    # Return the booking details.
    return {
        "id": booking.id,
        "slot_id": booking.slot_id,
        "name": booking.name,
        "email": booking.email,
        "status": booking.status,
        "created_at": booking.created_at
    }


@app.delete("/bookings/{booking_id}")
def cancel_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    # Find the booking.
    booking = db.query(Booking).filter(
        Booking.id == booking_id
    ).first()

    if not booking:
        raise HTTPException(
            status_code=404,
            detail="Booking not found"
        )

    # Prevent cancellation of an already cancelled booking.
    if booking.status == "cancelled":
        raise HTTPException(
            status_code=409,
            detail="Booking has already been cancelled"
        )

    # Update the booking status.
    booking.status = "cancelled"

    db.commit()
    db.refresh(booking)

    return {
        "id": booking.id,
        "slot_id": booking.slot_id,
        "name": booking.name,
        "email": booking.email,
        "status": booking.status,
        "created_at": booking.created_at
    }

@app.get("/bookings")
def get_bookings(
    email: str,
    limit: int = 10,
    cursor: str = None,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    # Check that the email address is not empty.
    email = email.strip().lower()

    if not email:
        raise HTTPException(
            status_code=400,
            detail="Email is required"
        )

    # Check that the limit is between 1 and 50.
    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=400,
            detail="Limit must be between 1 and 50"
        )
    # Find bookings belonging to this email address.
    query = db.query(Booking).filter(
        Booking.email == email
    )

    # Show the oldest bookings first.
    query = query.order_by(Booking.created_at.asc(), Booking.id.asc())

    # If a cursor is supplied, find bookings after that booking.
    if cursor:
        last_booking = db.query(Booking).filter(
            Booking.id == cursor
        ).first()

        if not last_booking or last_booking.email != email:
            raise HTTPException(
                status_code=400,
                detail="Invalid cursor"
            )

        query = query.filter(
            (Booking.created_at > last_booking.created_at) |
            (
                (Booking.created_at == last_booking.created_at) &
                (Booking.id > last_booking.id)
            )
        )

    # Retrieve one extra booking to check whether another page exists.
    bookings = query.limit(limit + 1).all()

    has_more = len(bookings) > limit

    if has_more:
        bookings = bookings[:limit]

    # Prepare the booking details for the response.
    booking_list = [
        {
            "id": booking.id,
            "slot_id": booking.slot_id,
            "name": booking.name,
            "email": booking.email,
            "status": booking.status,
            "created_at": booking.created_at
        }
        for booking in bookings
    ]

    # Set the cursor for the next page, if one exists.
    next_cursor = bookings[-1].id if has_more else None

    return {
        "bookings": booking_list,
        "next_cursor": next_cursor
    }

