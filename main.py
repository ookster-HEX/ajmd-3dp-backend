# Standard Library Imports
import os
import re
import hmac
import uuid
import json
import math
import time
import base64
import shutil
import asyncio
import hashlib
import secrets
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Union
from contextlib import asynccontextmanager

# FastAPI & Starlette Core Imports
from fastapi import (
    FastAPI,
    APIRouter,
    Depends,
    HTTPException,
    status,
    Header,
    Request,
    Response,
    UploadFile,
    File,
    Form,
    Query,
    Path as FastAPIPath,
    BackgroundTasks
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse

# Pydantic Data Validation Imports
from pydantic import BaseModel, Field, field_validator, ConfigDict

# Async SQLAlchemy ORM & Database Imports
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    JSON,
    text,
    select,
    update,
    delete
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

# ==============================================================================
# 1. CONFIGURATION & CONSTANTS
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ajmd-3dp-backend")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./3dp.db")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-ajmd-3dp-secret-key")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 7)))  # 7 days

STORAGE_ROOT = Path("./storage")
USER_UPLOADS_DIR = STORAGE_ROOT / "useruploads"
SAMPLES_DIR = STORAGE_ROOT / "samples"

ALLOWED_EXTENSIONS = {".3mf", ".obj", ".step", ".gcode"}
ALLOWED_QUOTE_STATUSES = {"draft", "custom-pending", "custom-in-review", "approved"}
ALLOWED_SHIPPING_MODES = {"standard", "fast", "collection"}
ALLOWED_PAYMENT_STATUSES = {"pending", "paid", "canceled", "for refund", "refunded"}
SUPPORT_CODE_TTL_SECONDS = 600  # 10 minutes
ADMIN_SUPPORT_TOKEN_TTL_SECONDS = 1200  # 20 minutes

ORCA_SLICER_BIN = os.getenv("ORCA_SLICER_BIN", "orca-slicer")
ORCA_PROFILES_DIR = Path(os.getenv("ORCA_PROFILES_DIR", "./profiles"))


# ==============================================================================
# 2. DATABASE SETUP & MODELS
# ==============================================================================

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

Base = declarative_base()


class User(Base):
    """SQLAlchemy model for the 'users' table."""
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String, unique=True, index=True, nullable=False)
    passwordhash = Column(String, nullable=False)
    credits = Column(Float, default=0.0, nullable=False)
    firstname = Column(String, nullable=False)
    lastname = Column(String, nullable=False)
    address_line_1 = Column(String, nullable=True)
    address_line_2 = Column(String, nullable=True)
    city = Column(String, nullable=True)
    postcode = Column(String, nullable=True)
    pinhash = Column(String, nullable=False)
    admin = Column(Boolean, default=False, nullable=False)

    quotes = relationship("Quote", back_populates="owner", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="owner", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="owner", cascade="all, delete-orphan")


class Quote(Base):
    """SQLAlchemy model for the 'quotes' table."""
    __tablename__ = "quotes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    userid = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    duration = Column(Float, default=0.0, nullable=False)  # Duration in minutes
    slicer_settings = Column(JSON, default=dict, nullable=False)
    quote_metadata = Column(JSON, default=dict, nullable=False)
    status = Column(String, default="draft", index=True, nullable=False)
    cost = Column(Float, default=0.0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    owner = relationship("User", back_populates="quotes")


class Payment(Base):
    """SQLAlchemy model for the 'payments' table."""
    __tablename__ = "payments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    amount = Column(Float, default=0.0, nullable=False)
    date_created = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    status = Column(String, default="pending", index=True, nullable=False)

    owner = relationship("User", back_populates="payments")
    order = relationship("Order", back_populates="payment", uselist=False)


class Order(Base):
    """SQLAlchemy model for the 'orders' table."""
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    userid = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    payment_id = Column(String, ForeignKey("payments.id"), index=True, nullable=True)
    data = Column(JSON, default=dict, nullable=False)

    owner = relationship("User", back_populates="orders")
    payment = relationship("Payment", back_populates="order")


async def get_db() -> AsyncSession:
    """Dependency providing an asynchronous SQLAlchemy session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ==============================================================================
# 3. PYDANTIC SCHEMAS
# ==============================================================================

class SignupRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password_hash: str = Field(..., min_length=8)
    firstname: str = Field(..., min_length=1, max_length=64)
    lastname: str = Field(..., min_length=1, max_length=64)
    pinhash: str = Field(..., min_length=8)
    address_line_1: Optional[str] = None
    address_line_2: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password_hash: str


class AuthResponse(BaseModel):
    user_id: str
    id: str
    username: str
    password_hash: str
    token: str
    credits: float
    admin: bool = False


class CheckTokenRequest(BaseModel):
    token: Optional[str] = None


class SupportCodeResponse(BaseModel):
    support_code: str
    issued_at: datetime
    expires_at: datetime
    expires_in_seconds: int = SUPPORT_CODE_TTL_SECONDS


class AdminUserLookupResponse(BaseModel):
    exists: bool
    user_id: str
    username: str
    firstname: str
    lastname: str
    admin_token: str
    expires_at: datetime
    expires_in_seconds: int = ADMIN_SUPPORT_TOKEN_TTL_SECONDS


class AdminBalanceResponse(BaseModel):
    user_id: str
    username: str
    credits: float


class AdminBalanceDepositRequest(BaseModel):
    amount: float = Field(..., gt=0.0)


class AdminQuoteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    userid: str
    duration: Optional[float] = 0.0
    slicer_settings: Optional[Dict[str, Any]] = Field(default_factory=dict)
    quote_metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    status: Optional[str] = "draft"
    cost: Optional[float] = 0.0

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> str:
        if v is None:
            return "draft"
        if v not in ALLOWED_QUOTE_STATUSES:
            raise ValueError(f"Invalid status '{v}'. Must be one of {sorted(ALLOWED_QUOTE_STATUSES)}")
        return v


class AdminQuoteUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    userid: Optional[str] = None
    duration: Optional[float] = None
    slicer_settings: Optional[Dict[str, Any]] = None
    quote_metadata: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    cost: Optional[float] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALLOWED_QUOTE_STATUSES:
            raise ValueError(f"Invalid status '{v}'. Must be one of {sorted(ALLOWED_QUOTE_STATUSES)}")
        return v


class UserUpdateRequest(BaseModel):
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    address_line_1: Optional[str] = None
    address_line_2: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None
    pinhash: Optional[str] = None
    password_hash: Optional[str] = None


class UserPublicResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    credits: float
    firstname: str
    lastname: str
    address_line_1: Optional[str] = None
    address_line_2: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None
    admin: bool = False


class UserFindRequest(BaseModel):
    id: Optional[str] = None
    username: Optional[str] = None


class CreditPaymentRequest(BaseModel):
    amount: float = Field(..., gt=0)
    pinhash: str
    quote_id: Optional[str] = None


class QuoteUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    duration: Optional[float] = 0.0
    slicer_settings: Optional[Dict[str, Any]] = None
    quote_metadata: Optional[Dict[str, Any]] = None
    status: Optional[str] = "draft"
    cost: Optional[float] = 0.0

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> str:
        if v is None:
            return "draft"
        if v not in ALLOWED_QUOTE_STATUSES:
            raise ValueError(f"Invalid status '{v}'. Must be one of {sorted(ALLOWED_QUOTE_STATUSES)}")
        return v


class QuoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    userid: str
    duration: float
    slicer_settings: Dict[str, Any]
    quote_metadata: Dict[str, Any]
    status: str
    cost: float
    created_at: datetime


class QuoteFindRequest(BaseModel):
    id: Optional[str] = None
    userid: Optional[str] = None
    status: Optional[str] = None


class SliceRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    file_id: str
    slicemode: Optional[str] = "simple"
    mode: Optional[str] = "quality"  # cheap | strong | quality | custom
    speed: Optional[float] = 33.0
    quality: Optional[float] = 34.0
    strength: Optional[float] = 33.0
    settings: Optional[Dict[str, Any]] = Field(default_factory=dict)


class SliceCostResponse(BaseModel):
    cost: float


class OrderShippingInfo(BaseModel):
    shippingmode: str
    address: Optional[Dict[str, Any]] = None
    cost: float = 0.0

    @field_validator("shippingmode")
    @classmethod
    def validate_shipping_mode(cls, v: str) -> str:
        mode = v.strip().lower()
        if mode not in ALLOWED_SHIPPING_MODES:
            raise ValueError(f"Invalid shippingmode '{v}'. Must be one of {sorted(ALLOWED_SHIPPING_MODES)}")
        return mode


class OrderQuoteItem(BaseModel):
    quoteid: str
    quantity: int = Field(default=1, ge=1)
    additional_notes: Optional[str] = None


class OrderCreateRequest(BaseModel):
    shipping: OrderShippingInfo
    quotes: List[OrderQuoteItem] = Field(..., min_length=1)
    status: Optional[str] = "pending"


class OrderUpdateRequest(BaseModel):
    shipping: Optional[OrderShippingInfo] = None
    quotes: Optional[List[OrderQuoteItem]] = None
    status: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


class AdminOrderCreateRequest(BaseModel):
    id: Optional[str] = None
    userid: str
    payment_id: Optional[str] = None
    shipping: OrderShippingInfo
    quotes: List[OrderQuoteItem] = Field(..., min_length=1)
    status: Optional[str] = "pending"


class AdminOrderUpdateRequest(BaseModel):
    userid: Optional[str] = None
    payment_id: Optional[str] = None
    shipping: Optional[OrderShippingInfo] = None
    quotes: Optional[List[OrderQuoteItem]] = None
    status: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    userid: str
    payment_id: Optional[str] = None
    data: Dict[str, Any]


class OrderFindRequest(BaseModel):
    id: Optional[str] = None
    userid: Optional[str] = None
    payment_id: Optional[str] = None
    status: Optional[str] = None


class PaymentCreateRequest(BaseModel):
    amount: float = Field(..., ge=0.0)
    status: Optional[str] = "pending"

    @field_validator("status")
    @classmethod
    def validate_payment_status(cls, v: Optional[str]) -> str:
        if v is None:
            return "pending"
        if v not in ALLOWED_PAYMENT_STATUSES:
            raise ValueError(f"Invalid payment status '{v}'. Must be one of {sorted(ALLOWED_PAYMENT_STATUSES)}")
        return v


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user: str
    amount: float
    date_created: datetime
    status: str


class PaymentFindRequest(BaseModel):
    id: Optional[str] = None
    user: Optional[str] = None
    status: Optional[str] = None


# ==============================================================================
# 4. AUTHENTICATION & TOKEN HELPERS
# ==============================================================================

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_access_token(user_id: str, password_hash: str) -> str:
    """
    Generates an HMAC-SHA256 signed stateless session token containing
    user_id, password_hash fingerprint, and expiration timestamp.
    """
    payload = {
        "uid": user_id,
        "ph": hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16],
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
        "nonce": secrets.token_hex(8)
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def verify_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Verifies signature and expiration of an access token."""
    try:
        parts = token.strip().split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts
        expected_sig = hmac.new(
            SECRET_KEY.encode("utf-8"),
            payload_b64.encode("utf-8"),
            hashlib.sha256
        ).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def _derive_support_cipher_key() -> bytes:
    """Derives a 32-byte key from SECRET_KEY for reversible support code obfuscation."""
    return hashlib.sha256(f"support-code:{SECRET_KEY}".encode("utf-8")).digest()


def generate_support_code(user_id: str) -> tuple[str, datetime, datetime]:
    """
    Generates a backend-reversible, HMAC-authenticated support code encoding:
      - uid: User ID
      - iat: Current Unix timestamp
      - exp: Expiration Unix timestamp (+10 minutes)
    """
    now_dt = datetime.now(timezone.utc)
    exp_dt = datetime.fromtimestamp(now_dt.timestamp() + SUPPORT_CODE_TTL_SECONDS, tz=timezone.utc)

    payload = {
        "uid": user_id,
        "iat": int(now_dt.timestamp()),
        "exp": int(exp_dt.timestamp())
    }
    raw_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    key = _derive_support_cipher_key()
    keystream = (key * ((len(raw_bytes) // len(key)) + 1))[:len(raw_bytes)]
    cipher_bytes = bytes(a ^ b for a, b in zip(raw_bytes, keystream))

    sig = hmac.new(key, cipher_bytes, hashlib.sha256).digest()[:12]
    code = f"SUP-{_b64url_encode(cipher_bytes)}.{_b64url_encode(sig)}"
    return code, now_dt, exp_dt


def decode_support_code(support_code: str) -> Optional[Dict[str, Any]]:
    """
    Reverses and verifies a support code back into {uid, iat, exp} if valid and unexpired.
    """
    try:
        cleaned = support_code.strip()
        if not cleaned.startswith("SUP-"):
            return None
        body = cleaned[4:]
        cipher_b64, sig_b64 = body.split(".", 1)
        cipher_bytes = _b64url_decode(cipher_b64)
        actual_sig = _b64url_decode(sig_b64)

        key = _derive_support_cipher_key()
        expected_sig = hmac.new(key, cipher_bytes, hashlib.sha256).digest()[:12]
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        keystream = (key * ((len(cipher_bytes) // len(key)) + 1))[:len(cipher_bytes)]
        raw_bytes = bytes(a ^ b for a, b in zip(cipher_bytes, keystream))
        payload = json.loads(raw_bytes.decode("utf-8"))

        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def create_admin_support_token(target_user_id: str, admin_user_id: str) -> tuple[str, datetime]:
    """
    Creates a 20-minute signed token scoped to a specific target user for
    admin balance inspection and deposits.
    """
    exp_ts = int(time.time()) + ADMIN_SUPPORT_TOKEN_TTL_SECONDS
    exp_dt = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
    payload = {
        "scope": "admin_user_balance",
        "target_uid": target_user_id,
        "admin_uid": admin_user_id,
        "exp": exp_ts,
        "nonce": secrets.token_hex(6)
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    return f"ADM-{payload_b64}.{_b64url_encode(sig)}", exp_dt


def verify_admin_support_token(token: str) -> Optional[Dict[str, Any]]:
    """Verifies an ADM-... 20-minute scoped token and returns its payload."""
    try:
        cleaned = token.strip()
        if not cleaned.startswith("ADM-"):
            return None
        payload_b64, sig_b64 = cleaned[4:].split(".", 1)
        expected_sig = hmac.new(
            SECRET_KEY.encode("utf-8"),
            payload_b64.encode("utf-8"),
            hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected_sig, _b64url_decode(sig_b64)):
            return None

        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if payload.get("scope") != "admin_user_balance":
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


async def get_current_user(
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None, alias="X-Auth-Token"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_password_hash: Optional[str] = Header(default=None, alias="X-User-Password-Hash"),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Authenticates requests using either:
      1) Bearer token in Authorization header or X-Auth-Token header (primary)
      2) Fallback X-User-Id + X-User-Password-Hash headers
    """
    token: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif x_auth_token:
        token = x_auth_token.strip()

    # Primary check: Token authentication
    if token:
        payload = verify_access_token(token)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired authentication token."
            )
        user_id = payload.get("uid")
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authenticated user no longer exists."
            )
        expected_ph_prefix = hashlib.sha256(user.passwordhash.encode("utf-8")).hexdigest()[:16]
        if not hmac.compare_digest(expected_ph_prefix, payload.get("ph", "")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token invalidated due to credential change."
            )
        return user

    # Secondary fallback check: X-User-Id + X-User-Password-Hash
    if x_user_id and x_user_password_hash:
        result = await db.execute(select(User).where(User.id == x_user_id.strip()))
        user = result.scalar_one_or_none()
        if not user or not hmac.compare_digest(user.passwordhash, x_user_password_hash.strip()):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user ID or password hash."
            )
        return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing authentication credentials."
    )


async def get_current_admin_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """Ensures the authenticated user has admin=True in the database."""
    if not bool(getattr(current_user, "admin", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required."
        )
    return current_user


# ==============================================================================
# 5. FILE & ORCASLICER HELPERS
# ==============================================================================

def sanitize_id(raw_id: str) -> str:
    """Prevents directory traversal by ensuring IDs contain safe characters only."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", raw_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid identifier format."
        )
    return raw_id


def find_uploaded_file(file_id: str) -> Optional[Path]:
    """Locates ./storage/useruploads/{file_id}.{ext} across allowed extensions."""
    safe_id = sanitize_id(file_id)
    for ext in ALLOWED_EXTENSIONS:
        candidate = USER_UPLOADS_DIR / f"{safe_id}{ext}"
        if candidate.is_file():
            return candidate
    return None


def parse_orcaslicer_duration_minutes(output_text: str, gcode_path: Optional[Path] = None) -> float:
    """
    Extracts estimated print duration in minutes from OrcaSlicer stdout/stderr
    or generated G-code header comments.
    """
    combined_text = output_text or ""
    if gcode_path and gcode_path.is_file():
        try:
            # Read first 8KB and last 8KB of G-code where OrcaSlicer writes metadata
            file_size = gcode_path.stat().st_size
            with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(8192)
                tail = ""
                if file_size > 8192:
                    f.seek(max(0, file_size - 8192))
                    tail = f.read(8192)
            combined_text += "\n" + head + "\n" + tail
        except Exception as exc:
            logger.warning(f"Failed reading generated G-code for metadata: {exc}")

    # Pattern 1: OrcaSlicer comment `; estimated printing time (normal mode) = 1h 23m 45s`
    time_line_match = re.search(
        r"estimated printing time[^=]*=\s*([^\r\n]+)",
        combined_text,
        re.IGNORECASE
    )
    if time_line_match:
        time_str = time_line_match.group(1)
        days = re.search(r"(\d+)\s*d", time_str)
        hours = re.search(r"(\d+)\s*h", time_str)
        mins = re.search(r"(\d+)\s*m", time_str)
        secs = re.search(r"(\d+)\s*s", time_str)
        total_minutes = (
            (int(days.group(1)) * 1440 if days else 0)
            + (int(hours.group(1)) * 60 if hours else 0)
            + (int(mins.group(1)) if mins else 0)
            + (int(secs.group(1)) / 60.0 if secs else 0.0)
        )
        if total_minutes > 0:
            return total_minutes

    # Pattern 2: Total seconds comment `; total estimated time: 5025`
    secs_match = re.search(r"total estimated time[:=]\s*(\d+(?:\.\d+)?)", combined_text, re.IGNORECASE)
    if secs_match:
        return float(secs_match.group(1)) / 60.0

    raise ValueError("Could not parse print duration from OrcaSlicer output.")


def calculate_cost_from_minutes(minutes: float) -> float:
    """
    Calculates cost as: time taken in minutes / 6, rounded to the nearest 0.1.
    """
    raw_cost = max(0.0, minutes) / 6.0
    return round(raw_cost, 1)


async def run_orcaslicer_and_get_cost(
    file_path: Path,
    slice_payload: SliceRequest,
    is_advanced: bool
) -> float:
    """
    Runs headless OrcaSlicer against the uploaded model file, extracts the print
    duration in minutes, and returns `round(minutes / 6.0, 1)`.
    """
    if file_path.suffix.lower() == ".gcode":
        # If the user uploaded pre-sliced G-code, extract its duration directly
        minutes = parse_orcaslicer_duration_minutes("", gcode_path=file_path)
        return calculate_cost_from_minutes(minutes)

    with tempfile.TemporaryDirectory(prefix="orca_slice_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        output_gcode = tmp_dir / "output.gcode"

        cmd: List[str] = [
            ORCA_SLICER_BIN,
            "--slice", "0",
            "--outputdir", str(tmp_dir),
            "--export-gcode", str(output_gcode)
        ]

        # Attach optional JSON profiles if configured in ./profiles
        machine_profile = ORCA_PROFILES_DIR / "machine.json"
        filament_profile = ORCA_PROFILES_DIR / "filament.json"
        process_profile = ORCA_PROFILES_DIR / f"{slice_payload.mode or 'quality'}.json"

        settings_files = [
            str(p) for p in (machine_profile, filament_profile, process_profile) if p.is_file()
        ]
        if settings_files:
            cmd.extend(["--load-settings", ";".join(settings_files)])

        # Write dynamic overrides for simple or advanced slicing modes
        override_dict: Dict[str, Any] = {}
        if not is_advanced:
            # Map simple sliders (0..100) to sensible OrcaSlicer parameters
            speed_val = slice_payload.speed if slice_payload.speed is not None else 33.0
            quality_val = slice_payload.quality if slice_payload.quality is not None else 34.0
            strength_val = slice_payload.strength if slice_payload.strength is not None else 33.0

            layer_height = round(max(0.08, 0.28 - (quality_val / 100.0) * 0.16), 2)
            infill_density = f"{int(min(100, max(10, 15 + strength_val * 0.7)))}%"
            outer_wall_speed = int(60 + (speed_val / 100.0) * 140)

            override_dict.update({
                "layer_height": str(layer_height),
                "sparse_infill_density": infill_density,
                "outer_wall_speed": str(outer_wall_speed)
            })
        else:
            # Merge any explicit advanced slicer settings provided by the frontend
            if slice_payload.settings:
                override_dict.update(slice_payload.settings)

        if override_dict:
            override_path = tmp_dir / "overrides.json"
            override_path.write_text(json.dumps(override_dict), encoding="utf-8")
            cmd.extend(["--load-settings", str(override_path)])

        cmd.append(str(file_path))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=120.0)
        except FileNotFoundError:
            logger.error(f"OrcaSlicer binary not found at '{ORCA_SLICER_BIN}'.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Slicer engine is unavailable on the server."
            )
        except asyncio.TimeoutError:
            process.kill()
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="OrcaSlicer timed out while processing the model."
            )

        stdout_str = stdout_bytes.decode("utf-8", errors="ignore")
        stderr_str = stderr_bytes.decode("utf-8", errors="ignore")

        if process.returncode != 0:
            logger.error(f"OrcaSlicer failed (exit {process.returncode}): {stderr_str}")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"OrcaSlicer failed to slice model: {stderr_str.strip() or 'Unknown error'}"
            )

        # Locate generated gcode file in tmp_dir if not strictly named output.gcode
        if not output_gcode.is_file():
            gcodes = list(tmp_dir.glob("*.gcode"))
            if gcodes:
                output_gcode = gcodes[0]

        try:
            minutes = parse_orcaslicer_duration_minutes(
                f"{stdout_str}\n{stderr_str}",
                gcode_path=output_gcode if output_gcode.is_file() else None
            )
        except ValueError as exc:
            logger.error(f"Duration parse error: {exc}\nStdout: {stdout_str}\nStderr: {stderr_str}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Slicing succeeded, but print duration could not be determined."
            )

        return calculate_cost_from_minutes(minutes)


# ==============================================================================
# 6. FASTAPI APPLICATION LIFESPAN & MIDDLEWARE
# ==============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensures storage directories and SQLite database tables exist at startup."""
    USER_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    ORCA_PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Gracefully add 'admin' column to existing SQLite databases if missing
        if "sqlite" in DATABASE_URL:
            res = await conn.execute(text("PRAGMA table_info(users)"))
            cols = {row[1] for row in res.fetchall()}
            if "admin" not in cols:
                await conn.execute(text("ALTER TABLE users ADD COLUMN admin BOOLEAN NOT NULL DEFAULT 0"))

    logger.info("Database tables and storage directories initialized.")
    yield
    await engine.dispose()


app = FastAPI(
    title="AJMD 3D Printing Quoting Backend",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Auth-Token",
        "X-User-Id",
        "X-User-Password-Hash",
        "X-Pin-Hash",
        "X-Admin-User-Token",
        "Accept",
        "Origin"
    ],
)


# ==============================================================================
# 7. ROUTERS DEFINITION (/users/, /files/, /quoting/, /orders/, /payments/, /admin/)
# ==============================================================================

users_router = APIRouter(prefix="/users", tags=["Users"])
users_auth_router = APIRouter(prefix="/auth", tags=["Users Auth"])

files_router = APIRouter(prefix="/files", tags=["Files"])
files_samples_router = APIRouter(prefix="/samples", tags=["Sample Files"])

quoting_router = APIRouter(prefix="/quoting", tags=["Quoting"])
quoting_quotes_router = APIRouter(prefix="/quotes", tags=["Quotes"])
quoting_gen_router = APIRouter(prefix="/gen", tags=["Quote Generation"])

orders_router = APIRouter(prefix="/orders", tags=["Orders"])
payments_router = APIRouter(prefix="/payments", tags=["Payments"])
admin_router = APIRouter(prefix="/admin", tags=["Admin"])


# ------------------------------------------------------------------------------
# 7A. /users/auth/ ENDPOINTS (login, signup, checktoken)
# ------------------------------------------------------------------------------

@users_auth_router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
    """Registers a new user with 0 starting credits and returns session credentials."""
    existing = await db.execute(select(User).where(User.username == payload.username.strip()))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username is already taken."
        )

    new_user = User(
        id=str(uuid.uuid4()),
        username=payload.username.strip(),
        passwordhash=payload.password_hash.strip(),
        credits=0.0,
        firstname=payload.firstname.strip(),
        lastname=payload.lastname.strip(),
        address_line_1=payload.address_line_1,
        address_line_2=payload.address_line_2,
        city=payload.city,
        postcode=payload.postcode,
        pinhash=payload.pinhash.strip(),
        admin=False
    )

    try:
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists.")
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Database error during signup: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error.")

    token = create_access_token(new_user.id, new_user.passwordhash)
    return AuthResponse(
        user_id=new_user.id,
        id=new_user.id,
        username=new_user.username,
        password_hash=new_user.passwordhash,
        token=token,
        credits=new_user.credits,
        admin=bool(new_user.admin)
    )


@users_auth_router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticates a user by username and SHA-256 password hash, returning a token."""
    result = await db.execute(select(User).where(User.username == payload.username.strip()))
    user = result.scalar_one_or_none()

    if not user or not hmac.compare_digest(user.passwordhash, payload.password_hash.strip()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password."
        )

    token = create_access_token(user.id, user.passwordhash)
    return AuthResponse(
        user_id=user.id,
        id=user.id,
        username=user.username,
        password_hash=user.passwordhash,
        token=token,
        credits=user.credits,
        admin=bool(user.admin)
    )


@users_auth_router.api_route("/checktoken", methods=["GET", "POST"])
async def check_token(
    body: Optional[CheckTokenRequest] = None,
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None, alias="X-Auth-Token"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_password_hash: Optional[str] = Header(default=None, alias="X-User-Password-Hash"),
    db: AsyncSession = Depends(get_db)
):
    """Validates whether the provided session token (or fallback credentials) is valid."""
    raw_token = None
    if body and body.token:
        raw_token = body.token.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization.split(" ", 1)[1].strip()
    elif x_auth_token:
        raw_token = x_auth_token.strip()

    if raw_token:
        payload = verify_access_token(raw_token)
        if not payload:
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"valid": False})
        result = await db.execute(select(User).where(User.id == payload.get("uid")))
        user = result.scalar_one_or_none()
        if not user:
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"valid": False})
        expected_ph_prefix = hashlib.sha256(user.passwordhash.encode("utf-8")).hexdigest()[:16]
        if not hmac.compare_digest(expected_ph_prefix, payload.get("ph", "")):
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"valid": False})
        return {
            "valid": True,
            "user_id": user.id,
            "username": user.username,
            "admin": bool(getattr(user, "admin", False))
        }

    if x_user_id and x_user_password_hash:
        result = await db.execute(select(User).where(User.id == x_user_id.strip()))
        user = result.scalar_one_or_none()
        if user and hmac.compare_digest(user.passwordhash, x_user_password_hash.strip()):
            return {
                "valid": True,
                "user_id": user.id,
                "username": user.username,
                "admin": bool(getattr(user, "admin", False))
            }

    return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"valid": False})


# ------------------------------------------------------------------------------
# 7B. /users/ ENDPOINTS (supportcode, find, {id})
# Note: /supportcode and /find MUST be registered before /{id}
# ------------------------------------------------------------------------------

@users_router.get("/supportcode", response_model=SupportCodeResponse)
async def get_support_code(
    current_user: User = Depends(get_current_user)
):
    """
    Generates a time-limited (10-minute) reversible support code containing
    the authenticated user's ID, current timestamp, and expiration timestamp.
    """
    code, issued_at, expires_at = generate_support_code(current_user.id)
    return SupportCodeResponse(
        support_code=code,
        issued_at=issued_at,
        expires_at=expires_at,
        expires_in_seconds=SUPPORT_CODE_TTL_SECONDS
    )


@users_router.api_route("/find", methods=["GET", "POST"], response_model=List[UserPublicResponse])
async def find_users(
    id: Optional[str] = Query(default=None),
    username: Optional[str] = Query(default=None),
    body: Optional[UserFindRequest] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Searches users by ID or username."""
    search_id = (body.id if body and body.id else id)
    search_username = (body.username if body and body.username else username)

    stmt = select(User)
    if search_id:
        stmt = stmt.where(User.id == search_id.strip())
    if search_username:
        stmt = stmt.where(User.username == search_username.strip())

    result = await db.execute(stmt)
    users = result.scalars().all()
    return users


@users_router.get("/{id}", response_model=UserPublicResponse)
async def get_user_by_id(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Retrieves a user profile by ID."""
    result = await db.execute(select(User).where(User.id == id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return user


@users_router.patch("/{id}", response_model=UserPublicResponse)
async def update_user_by_id(
    id: str,
    payload: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Updates profile fields (name, address, pinhash, passwordhash) for the authenticated user."""
    if current_user.id != id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another user's profile.")

    update_data = payload.model_dump(exclude_unset=True)
    if "password_hash" in update_data:
        current_user.passwordhash = update_data.pop("password_hash")
    for field_name, value in update_data.items():
        setattr(current_user, field_name, value)

    try:
        await db.commit()
        await db.refresh(current_user)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Error updating user {id}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update user.")

    return current_user


@users_router.post("/{id}", response_model=UserPublicResponse)
async def pay_with_credits(
    id: str,
    payment: CreditPaymentRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Processes a credit payment when placing an order: verifies the user's SHA-256
    pinhash and deducts the requested credits from their balance.
    """
    if current_user.id != id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot spend another user's credits.")

    if not hmac.compare_digest(current_user.pinhash, payment.pinhash.strip()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid PIN hash.")

    if current_user.credits < payment.amount:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Insufficient credits.")

    current_user.credits = round(current_user.credits - payment.amount, 2)

    if payment.quote_id:
        q_res = await db.execute(select(Quote).where(Quote.id == payment.quote_id, Quote.userid == current_user.id))
        quote = q_res.scalar_one_or_none()
        if quote:
            quote.status = "approved"

    try:
        await db.commit()
        await db.refresh(current_user)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Credit deduction error for user {id}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Payment processing failed.")

    return current_user


# ------------------------------------------------------------------------------
# 7C. /files/ ENDPOINTS (samples/{sample_num}, {id})
# Note: /samples MUST be registered before /{id}
# ------------------------------------------------------------------------------

@files_samples_router.get("/{sample_num}")
async def get_sample_file(sample_num: int):
    """Streams a pre-configured sample file from ./storage/samples/sample{sample_num}.{ext}."""
    if sample_num < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sample number.")

    for ext in ALLOWED_EXTENSIONS:
        candidate = SAMPLES_DIR / f"sample{sample_num}{ext}"
        if candidate.is_file():
            return FileResponse(
                path=candidate,
                filename=candidate.name,
                media_type="application/octet-stream"
            )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Sample #{sample_num} not found."
    )


@files_router.post("/{id}", status_code=status.HTTP_201_CREATED)
async def upload_user_file(
    id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """
    Uploads a 3D model or G-code file (.3mf, .obj, .step, .gcode) and stores it
    at ./storage/useruploads/{id}.{ext}.
    """
    safe_id = sanitize_id(id)
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file has no filename.")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{ext}'. Allowed types: {sorted(ALLOWED_EXTENSIONS)}"
        )

    # Remove any previous file with the same ID under a different extension
    for existing_ext in ALLOWED_EXTENSIONS:
        old_file = USER_UPLOADS_DIR / f"{safe_id}{existing_ext}"
        if old_file.exists():
            old_file.unlink(missing_ok=True)

    dest_path = USER_UPLOADS_DIR / f"{safe_id}{ext}"

    try:
        with open(dest_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):  # 1MB chunks (no total size limit)
                buffer.write(chunk)
    except Exception as exc:
        logger.error(f"Failed saving upload {dest_path}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to store uploaded file.")
    finally:
        await file.close()

    return {
        "file_id": safe_id,
        "filename": dest_path.name,
        "extension": ext,
        "size_bytes": dest_path.stat().st_size
    }


@files_router.get("/{id}")
async def download_user_file(
    id: str,
    current_user: User = Depends(get_current_user)
):
    """Downloads a previously uploaded file from ./storage/useruploads/{id}.{ext}."""
    found = find_uploaded_file(id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    return FileResponse(
        path=found,
        filename=found.name,
        media_type="application/octet-stream"
    )


# ------------------------------------------------------------------------------
# 7D. /quoting/quotes/ ENDPOINTS (find, {id})
# Note: /find MUST be registered before /{id}
# ------------------------------------------------------------------------------

@quoting_quotes_router.api_route("/find", methods=["GET", "POST"], response_model=List[QuoteResponse])
async def find_quotes(
    id: Optional[str] = Query(default=None),
    userid: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    body: Optional[QuoteFindRequest] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Queries quotes filtered by quote ID, user ID, and/or status."""
    q_id = body.id if body and body.id else id
    q_userid = body.userid if body and body.userid else userid
    q_status = body.status if body and body.status else status_filter

    stmt = select(Quote)
    if q_id:
        stmt = stmt.where(Quote.id == q_id.strip())
    if q_userid:
        stmt = stmt.where(Quote.userid == q_userid.strip())
    if q_status:
        stmt = stmt.where(Quote.status == q_status.strip())

    stmt = stmt.order_by(Quote.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@quoting_quotes_router.get("/{id}", response_model=QuoteResponse)
async def get_quote_by_id(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Fetches a specific quote by ID."""
    result = await db.execute(select(Quote).where(Quote.id == id))
    quote = result.scalar_one_or_none()
    if not quote:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")
    return quote


@quoting_quotes_router.post("/{id}", response_model=QuoteResponse)
async def create_or_update_quote(
    id: str,
    payload: QuoteUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Creates or updates a quote by client-generated UUID {id}.
    Accepts structured fields (duration, slicer_settings, quote_metadata, status, cost)
    as well as flat form fields from the frontend quote/custom-quote drawers.
    """
    safe_id = sanitize_id(id)
    raw_extra = payload.model_extra or {}

    # Separate slicer parameters from general quote metadata if flat fields were submitted
    slicer_keys = {
        "slicemode", "mode", "speed", "quality", "strength",
        "layer_height", "infill", "material", "supports", "nozzle_size"
    }
    inferred_slicer_settings = {k: v for k, v in raw_extra.items() if k in slicer_keys}
    inferred_metadata = {k: v for k, v in raw_extra.items() if k not in slicer_keys}

    merged_slicer_settings = {**inferred_slicer_settings, **(payload.slicer_settings or {})}
    merged_metadata = {**inferred_metadata, **(payload.quote_metadata or {})}

    result = await db.execute(select(Quote).where(Quote.id == safe_id))
    existing_quote = result.scalar_one_or_none()

    if existing_quote:
        if existing_quote.userid != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another user's quote.")

        if payload.duration is not None:
            existing_quote.duration = payload.duration
        if merged_slicer_settings:
            existing_quote.slicer_settings = {**existing_quote.slicer_settings, **merged_slicer_settings}
        if merged_metadata:
            existing_quote.quote_metadata = {**existing_quote.quote_metadata, **merged_metadata}
        if payload.status is not None:
            existing_quote.status = payload.status
        if payload.cost is not None:
            existing_quote.cost = payload.cost

        quote_obj = existing_quote
    else:
        quote_obj = Quote(
            id=safe_id,
            userid=current_user.id,
            duration=payload.duration or 0.0,
            slicer_settings=merged_slicer_settings,
            quote_metadata=merged_metadata,
            status=payload.status or "draft",
            cost=payload.cost or 0.0
        )
        db.add(quote_obj)

    try:
        await db.commit()
        await db.refresh(quote_obj)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Database error saving quote {safe_id}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save quote.")

    return quote_obj


@quoting_quotes_router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_quote(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Deletes a quote owned by the authenticated user."""
    result = await db.execute(select(Quote).where(Quote.id == id))
    quote = result.scalar_one_or_none()
    if not quote:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")
    if quote.userid != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another user's quote.")

    await db.delete(quote)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------
# 7E. /quoting/gen/ ENDPOINTS (simpleslice, advancedslice)
# ------------------------------------------------------------------------------

@quoting_gen_router.post("/simpleslice", response_model=SliceCostResponse)
async def generate_simple_slice(
    payload: SliceRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Runs headless OrcaSlicer using simple tier/slider presets and returns
    the calculated cost only (minutes / 6 rounded to the nearest 0.1).
    """
    file_path = find_uploaded_file(payload.file_id)
    if not file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Uploaded model file not found.")

    cost = await run_orcaslicer_and_get_cost(file_path, payload, is_advanced=False)
    return SliceCostResponse(cost=cost)


@quoting_gen_router.post("/advancedslice", response_model=SliceCostResponse)
async def generate_advanced_slice(
    payload: SliceRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Runs headless OrcaSlicer using custom/advanced slicer parameters and returns
    the calculated cost only (minutes / 6 rounded to the nearest 0.1).
    """
    file_path = find_uploaded_file(payload.file_id)
    if not file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Uploaded model file not found.")

    cost = await run_orcaslicer_and_get_cost(file_path, payload, is_advanced=True)
    return SliceCostResponse(cost=cost)


# ------------------------------------------------------------------------------
# 7F. /orders/ ENDPOINTS (find, {id})
# Note: /find MUST be registered before /{id}
# ------------------------------------------------------------------------------

@orders_router.api_route("/find", methods=["GET", "POST"], response_model=List[OrderResponse])
async def find_orders(
    id: Optional[str] = Query(default=None),
    userid: Optional[str] = Query(default=None),
    payment_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    body: Optional[OrderFindRequest] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Searches orders filtered by order ID, user ID, payment ID, and/or JSON status."""
    o_id = body.id if body and body.id else id
    o_userid = body.userid if body and body.userid else userid
    o_payment_id = body.payment_id if body and body.payment_id else payment_id
    o_status = body.status if body and body.status else status_filter

    stmt = select(Order)
    if o_id:
        stmt = stmt.where(Order.id == o_id.strip())
    if o_userid:
        stmt = stmt.where(Order.userid == o_userid.strip())
    if o_payment_id:
        stmt = stmt.where(Order.payment_id == o_payment_id.strip())

    result = await db.execute(stmt)
    orders = result.scalars().all()

    if o_status:
        target_status = o_status.strip()
        orders = [o for o in orders if isinstance(o.data, dict) and o.data.get("status") == target_status]

    return orders


@orders_router.get("/{id}", response_model=OrderResponse)
async def get_order_by_id(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Retrieves an order by ID."""
    result = await db.execute(select(Order).where(Order.id == id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    if order.userid != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view another user's order.")
    return order


@orders_router.post("/{id}", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    id: str,
    payload: OrderCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Places a new order with client-generated UUID {id}:
      - Validates all referenced quotes exist, belong to the user, and are 'approved'
        (without altering quote statuses so quotes remain reusable).
      - Adds shipping as a line item and calculates total_cost.
      - Automatically creates a linked 'pending' payment record in the payments table.
    """
    safe_id = sanitize_id(id)

    existing_res = await db.execute(select(Order).where(Order.id == safe_id))
    if existing_res.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Order ID already exists.")

    quote_ids = [item.quoteid for item in payload.quotes]
    quotes_res = await db.execute(select(Quote).where(Quote.id.in_(quote_ids)))
    quotes_by_id = {q.id: q for q in quotes_res.scalars().all()}

    items_cost = 0.0
    serialized_quotes: List[Dict[str, Any]] = []

    for item in payload.quotes:
        quote = quotes_by_id.get(item.quoteid)
        if not quote:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Quote '{item.quoteid}' not found."
            )
        if quote.userid != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Quote '{item.quoteid}' does not belong to the authenticated user."
            )
        if quote.status != "approved":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Quote '{item.quoteid}' must have status 'approved' before ordering (current: '{quote.status}')."
            )

        line_total = round(quote.cost * item.quantity, 2)
        items_cost += line_total
        serialized_quotes.append({
            "quoteid": item.quoteid,
            "quantity": item.quantity,
            "additional_notes": item.additional_notes,
            "unit_cost": quote.cost,
            "line_total": line_total
        })

    # Resolve shipping address (defaulting to user's profile address if omitted)
    shipping_address = payload.shipping.address or {
        "address_line_1": current_user.address_line_1,
        "address_line_2": current_user.address_line_2,
        "city": current_user.city,
        "postcode": current_user.postcode
    }
    shipping_cost = round(max(0.0, payload.shipping.cost), 2)
    total_cost = round(items_cost + shipping_cost, 2)

    # Create associated pending payment record
    payment_id = str(uuid.uuid4())
    new_payment = Payment(
        id=payment_id,
        user=current_user.id,
        amount=total_cost,
        date_created=datetime.now(timezone.utc),
        status="pending"
    )

    order_data: Dict[str, Any] = {
        "date_placed": datetime.now(timezone.utc).isoformat(),
        "shipping": {
            "shippingmode": payload.shipping.shippingmode,
            "address": shipping_address,
            "cost": shipping_cost
        },
        "quotes": serialized_quotes,
        "status": payload.status or "pending",
        "total_cost": total_cost
    }

    new_order = Order(
        id=safe_id,
        userid=current_user.id,
        payment_id=payment_id,
        data=order_data
    )

    try:
        db.add(new_payment)
        db.add(new_order)
        await db.commit()
        await db.refresh(new_order)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Database error creating order {safe_id}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create order.")

    return new_order


@orders_router.patch("/{id}", response_model=OrderResponse)
async def modify_order(
    id: str,
    payload: OrderUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Modifies an existing order or cancels/soft-deletes it by updating data.status."""
    result = await db.execute(select(Order).where(Order.id == id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    if order.userid != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another user's order.")

    updated_data = dict(order.data or {})

    if payload.data:
        updated_data.update(payload.data)
    if payload.shipping is not None:
        updated_data["shipping"] = {
            "shippingmode": payload.shipping.shippingmode,
            "address": payload.shipping.address or updated_data.get("shipping", {}).get("address"),
            "cost": round(max(0.0, payload.shipping.cost), 2)
        }
    if payload.quotes is not None:
        updated_data["quotes"] = [q.model_dump() for q in payload.quotes]
    if payload.status is not None:
        updated_data["status"] = payload.status

    order.data = updated_data

    try:
        await db.commit()
        await db.refresh(order)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Database error updating order {id}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update order.")

    return order


# ------------------------------------------------------------------------------
# 7G. /payments/ ENDPOINTS (find, {id})
# Note: /find MUST be registered before /{id}
# ------------------------------------------------------------------------------

@payments_router.api_route("/find", methods=["GET", "POST"], response_model=List[PaymentResponse])
async def find_payments(
    id: Optional[str] = Query(default=None),
    user: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    body: Optional[PaymentFindRequest] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Searches payments filtered by payment ID, user ID, and/or status."""
    p_id = body.id if body and body.id else id
    p_user = body.user if body and body.user else user
    p_status = body.status if body and body.status else status_filter

    stmt = select(Payment)
    if p_id:
        stmt = stmt.where(Payment.id == p_id.strip())
    if p_user:
        stmt = stmt.where(Payment.user == p_user.strip())
    if p_status:
        stmt = stmt.where(Payment.status == p_status.strip())

    stmt = stmt.order_by(Payment.date_created.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@payments_router.get("/{id}", response_model=PaymentResponse)
async def get_payment_by_id(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Retrieves a payment record by ID."""
    result = await db.execute(select(Payment).where(Payment.id == id))
    payment = result.scalar_one_or_none()
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found.")
    if payment.user != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view another user's payment.")
    return payment


# ------------------------------------------------------------------------------
# 7H. /admin/ ENDPOINTS (quotes, orders, user, user/balance)
# All endpoints require an authenticated user with admin == True
# ------------------------------------------------------------------------------

@admin_router.get("/quotes", response_model=List[QuoteResponse])
async def admin_list_quotes(
    id: Optional[str] = Query(default=None),
    userid: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    min_cost: Optional[float] = Query(default=None),
    max_cost: Optional[float] = Query(default=None),
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns all quotes with optional query filters (id, userid, status, min_cost, max_cost)."""
    stmt = select(Quote)
    if id:
        stmt = stmt.where(Quote.id == id.strip())
    if userid:
        stmt = stmt.where(Quote.userid == userid.strip())
    if status_filter:
        stmt = stmt.where(Quote.status == status_filter.strip())
    if min_cost is not None:
        stmt = stmt.where(Quote.cost >= min_cost)
    if max_cost is not None:
        stmt = stmt.where(Quote.cost <= max_cost)

    stmt = stmt.order_by(Quote.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@admin_router.post("/quotes", response_model=QuoteResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_quote(
    payload: AdminQuoteCreateRequest,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Creates a new quote on behalf of any user."""
    quote_id = sanitize_id(payload.id) if payload.id else str(uuid.uuid4())

    user_res = await db.execute(select(User).where(User.id == payload.userid.strip()))
    if not user_res.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found.")

    existing_res = await db.execute(select(Quote).where(Quote.id == quote_id))
    if existing_res.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Quote ID already exists.")

    new_quote = Quote(
        id=quote_id,
        userid=payload.userid.strip(),
        duration=payload.duration or 0.0,
        slicer_settings=payload.slicer_settings or {},
        quote_metadata=payload.quote_metadata or {},
        status=payload.status or "draft",
        cost=payload.cost or 0.0
    )

    try:
        db.add(new_quote)
        await db.commit()
        await db.refresh(new_quote)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Admin error creating quote: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create quote.")

    return new_quote


@admin_router.get("/quotes/{id}", response_model=QuoteResponse)
async def admin_get_quote(
    id: str,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Retrieves any quote by ID."""
    result = await db.execute(select(Quote).where(Quote.id == id))
    quote = result.scalar_one_or_none()
    if not quote:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")
    return quote


@admin_router.post("/quotes/{id}", response_model=QuoteResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_quote_with_id(
    id: str,
    payload: AdminQuoteCreateRequest,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Creates a quote with explicit path {id} on behalf of any user."""
    payload.id = id
    return await admin_create_quote(payload, admin_user=admin_user, db=db)


@admin_router.put("/quotes/{id}", response_model=QuoteResponse)
async def admin_update_quote(
    id: str,
    payload: AdminQuoteUpdateRequest,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Updates any quote's cost, duration, status (e.g. approving custom quotes), or metadata."""
    result = await db.execute(select(Quote).where(Quote.id == id))
    quote = result.scalar_one_or_none()
    if not quote:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found.")

    if payload.userid is not None:
        quote.userid = payload.userid.strip()
    if payload.duration is not None:
        quote.duration = payload.duration
    if payload.slicer_settings is not None:
        quote.slicer_settings = payload.slicer_settings
    if payload.quote_metadata is not None:
        quote.quote_metadata = payload.quote_metadata
    if payload.status is not None:
        quote.status = payload.status
    if payload.cost is not None:
        quote.cost = payload.cost

    try:
        await db.commit()
        await db.refresh(quote)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Admin error updating quote {id}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update quote.")

    return quote


@admin_router.get("/orders", response_model=List[OrderResponse])
async def admin_list_orders(
    id: Optional[str] = Query(default=None),
    userid: Optional[str] = Query(default=None),
    payment_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns all orders with optional query filters (id, userid, payment_id, status)."""
    stmt = select(Order)
    if id:
        stmt = stmt.where(Order.id == id.strip())
    if userid:
        stmt = stmt.where(Order.userid == userid.strip())
    if payment_id:
        stmt = stmt.where(Order.payment_id == payment_id.strip())

    result = await db.execute(stmt)
    orders = result.scalars().all()

    if status_filter:
        target_status = status_filter.strip()
        orders = [o for o in orders if isinstance(o.data, dict) and o.data.get("status") == target_status]

    return orders


@admin_router.post("/orders", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_order(
    payload: AdminOrderCreateRequest,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Creates a new order on behalf of any user and generates its linked pending payment."""
    order_id = sanitize_id(payload.id) if payload.id else str(uuid.uuid4())

    user_res = await db.execute(select(User).where(User.id == payload.userid.strip()))
    target_user = user_res.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found.")

    existing_res = await db.execute(select(Order).where(Order.id == order_id))
    if existing_res.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Order ID already exists.")

    quote_ids = [item.quoteid for item in payload.quotes]
    quotes_res = await db.execute(select(Quote).where(Quote.id.in_(quote_ids)))
    quotes_by_id = {q.id: q for q in quotes_res.scalars().all()}

    items_cost = 0.0
    serialized_quotes: List[Dict[str, Any]] = []
    for item in payload.quotes:
        quote = quotes_by_id.get(item.quoteid)
        if not quote:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Quote '{item.quoteid}' not found.")
        line_total = round(quote.cost * item.quantity, 2)
        items_cost += line_total
        serialized_quotes.append({
            "quoteid": item.quoteid,
            "quantity": item.quantity,
            "additional_notes": item.additional_notes,
            "unit_cost": quote.cost,
            "line_total": line_total
        })

    shipping_address = payload.shipping.address or {
        "address_line_1": target_user.address_line_1,
        "address_line_2": target_user.address_line_2,
        "city": target_user.city,
        "postcode": target_user.postcode
    }
    shipping_cost = round(max(0.0, payload.shipping.cost), 2)
    total_cost = round(items_cost + shipping_cost, 2)

    payment_id = payload.payment_id or str(uuid.uuid4())
    new_payment = Payment(
        id=payment_id,
        user=target_user.id,
        amount=total_cost,
        date_created=datetime.now(timezone.utc),
        status="pending"
    )

    new_order = Order(
        id=order_id,
        userid=target_user.id,
        payment_id=payment_id,
        data={
            "date_placed": datetime.now(timezone.utc).isoformat(),
            "shipping": {
                "shippingmode": payload.shipping.shippingmode,
                "address": shipping_address,
                "cost": shipping_cost
            },
            "quotes": serialized_quotes,
            "status": payload.status or "pending",
            "total_cost": total_cost
        }
    )

    try:
        db.add(new_payment)
        db.add(new_order)
        await db.commit()
        await db.refresh(new_order)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Admin error creating order: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create order.")

    return new_order


@admin_router.get("/orders/{id}", response_model=OrderResponse)
async def admin_get_order(
    id: str,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Retrieves any order by ID."""
    result = await db.execute(select(Order).where(Order.id == id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    return order


@admin_router.post("/orders/{id}", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_order_with_id(
    id: str,
    payload: AdminOrderCreateRequest,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Creates an order with explicit path {id} on behalf of any user."""
    payload.id = id
    return await admin_create_order(payload, admin_user=admin_user, db=db)


@admin_router.put("/orders/{id}", response_model=OrderResponse)
async def admin_update_order(
    id: str,
    payload: AdminOrderUpdateRequest,
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Updates any order's status, shipping, quotes, payment_id, or JSON data."""
    result = await db.execute(select(Order).where(Order.id == id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    if payload.userid is not None:
        order.userid = payload.userid.strip()
    if payload.payment_id is not None:
        order.payment_id = payload.payment_id.strip()

    updated_data = dict(order.data or {})
    if payload.data:
        updated_data.update(payload.data)
    if payload.shipping is not None:
        updated_data["shipping"] = {
            "shippingmode": payload.shipping.shippingmode,
            "address": payload.shipping.address or updated_data.get("shipping", {}).get("address"),
            "cost": round(max(0.0, payload.shipping.cost), 2)
        }
    if payload.quotes is not None:
        updated_data["quotes"] = [q.model_dump() for q in payload.quotes]
    if payload.status is not None:
        updated_data["status"] = payload.status

    order.data = updated_data

    try:
        await db.commit()
        await db.refresh(order)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Admin error updating order {id}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update order.")

    return order


@admin_router.get("/user", response_model=AdminUserLookupResponse)
async def admin_verify_support_code(
    support_code: Optional[str] = Query(default=None),
    code: Optional[str] = Query(default=None),
    x_support_code: Optional[str] = Header(default=None, alias="X-Support-Code"),
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Reverses a user's 10-minute support_code, confirms the user exists in the database,
    and returns their name plus a 20-minute scoped admin token (ADM-...) for balance operations.
    """
    raw_code = support_code or code or x_support_code
    if not raw_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing 'support_code' query parameter or 'X-Support-Code' header."
        )

    decoded = decode_support_code(raw_code)
    if not decoded or not decoded.get("uid"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired support code."
        )

    target_uid = decoded["uid"]
    result = await db.execute(select(User).where(User.id == target_uid))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User referenced by support code does not exist."
        )

    admin_token, expires_at = create_admin_support_token(target_user.id, admin_user.id)
    return AdminUserLookupResponse(
        exists=True,
        user_id=target_user.id,
        username=target_user.username,
        firstname=target_user.firstname,
        lastname=target_user.lastname,
        admin_token=admin_token,
        expires_at=expires_at,
        expires_in_seconds=ADMIN_SUPPORT_TOKEN_TTL_SECONDS
    )


def _extract_admin_user_token(
    admin_token_query: Optional[str],
    x_admin_user_token: Optional[str]
) -> Dict[str, Any]:
    raw_token = x_admin_user_token or admin_token_query
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing 20-minute user support token (pass via 'X-Admin-User-Token' header or 'token' query parameter)."
        )
    payload = verify_admin_support_token(raw_token)
    if not payload or not payload.get("target_uid"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired 20-minute admin user token."
        )
    return payload


@admin_router.get("/user/balance", response_model=AdminBalanceResponse)
async def admin_get_user_balance(
    token: Optional[str] = Query(default=None),
    x_admin_user_token: Optional[str] = Header(default=None, alias="X-Admin-User-Token"),
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Views the target user's current credit balance using the 20-minute token from GET /admin/user."""
    payload = _extract_admin_user_token(token, x_admin_user_token)
    result = await db.execute(select(User).where(User.id == payload["target_uid"]))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found.")

    return AdminBalanceResponse(
        user_id=target_user.id,
        username=target_user.username,
        credits=target_user.credits
    )


@admin_router.put("/user/balance", response_model=AdminBalanceResponse)
async def admin_deposit_user_balance(
    deposit: AdminBalanceDepositRequest,
    token: Optional[str] = Query(default=None),
    x_admin_user_token: Optional[str] = Header(default=None, alias="X-Admin-User-Token"),
    admin_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db)
):
    """Deposits credits into the target user's balance using the 20-minute token from GET /admin/user."""
    payload = _extract_admin_user_token(token, x_admin_user_token)
    result = await db.execute(select(User).where(User.id == payload["target_uid"]))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found.")

    target_user.credits = round(target_user.credits + deposit.amount, 2)

    try:
        await db.commit()
        await db.refresh(target_user)
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.error(f"Admin balance deposit error for user {target_user.id}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to deposit credits.")

    return AdminBalanceResponse(
        user_id=target_user.id,
        username=target_user.username,
        credits=target_user.credits
    )


# ==============================================================================
# 8. MOUNT ROUTERS INTO MAIN APPLICATION
# ==============================================================================

users_router.include_router(users_auth_router)
files_router.include_router(files_samples_router)
quoting_router.include_router(quoting_quotes_router)
quoting_router.include_router(quoting_gen_router)

app.include_router(users_router)
app.include_router(files_router)
app.include_router(quoting_router)
app.include_router(orders_router)
app.include_router(payments_router)
app.include_router(admin_router)