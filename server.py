from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from fastapi import FastAPI, APIRouter, HTTPException, Depends, status
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import re
import asyncio
import logging
import random
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
import bcrypt
import jwt
import razorpay
import httpx
from recommendation_engine import recommendation_engine
from contextlib import asynccontextmanager
from fastapi import UploadFile, File
from s3 import upload_image_to_s3


# Configure logging early
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv('.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# --- Stock reaper for abandoned online payments ---
# /orders reserves (decrements) stock the moment an order is created,
# before Razorpay even opens — correct for preventing overselling while
# checkout is in progress, but if the shopper dismisses the payment
# sheet or never completes it, that stock was never released. A
# popular item could show "out of stock" purely from a handful of
# abandoned payment attempts, with no expiry to correct it.
#
# Runs as a background task in this same process (started from the
# lifespan hook below) rather than a separate worker/cron — this app
# has no task-queue infrastructure (no Celery/APScheduler dependency),
# and the cleanup query is idempotent (filtered by status + age), so it
# is safe to run redundantly if multiple app instances each run their
# own copy of this loop.
STOCK_REAPER_INTERVAL_SECONDS = 5 * 60
STOCK_REAPER_ABANDON_AFTER_MINUTES = 30

async def reap_abandoned_payments():
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STOCK_REAPER_ABANDON_AFTER_MINUTES)).isoformat()
    stale = await db.orders.find({
        "payment_method": "online",
        "payment_status": "pending",
        "order_status": "pending",
        "created_at": {"$lt": cutoff},
    }, {"_id": 0}).to_list(500)
    for order in stale:
        await restore_order_stock(order)
        await db.orders.update_one(
            {"id": order["id"]},
            {"$set": {
                "order_status": "cancelled",
                "cancel_reason": "Payment not completed within 30 minutes — stock released automatically.",
            }}
        )
    if stale:
        logging.info(f"Stock reaper: released stock for {len(stale)} abandoned order(s).")

async def _stock_reaper_loop():
    while True:
        try:
            await reap_abandoned_payments()
        except Exception as e:
            logging.error(f"Stock reaper failed: {e}")
        await asyncio.sleep(STOCK_REAPER_INTERVAL_SECONDS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    reaper_task = asyncio.create_task(_stock_reaper_loop())
    yield
    reaper_task.cancel()
    client.close()

app = FastAPI(lifespan=lifespan)

api_router = APIRouter(prefix="/api")
security = HTTPBearer()

JWT_SECRET = os.environ.get('JWT_SECRET')
if not JWT_SECRET:
    raise ValueError("JWT_SECRET environment variable is required")
JWT_ALGORITHM = 'HS256'


GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
if not GOOGLE_CLIENT_ID:
    logger.warning("GOOGLE_CLIENT_ID not set — Google sign-in will fail")



# Razorpay client - keys can be empty for COD-only mode
razorpay_key_id = os.environ.get('RAZORPAY_KEY_ID', '')
razorpay_key_secret = os.environ.get('RAZORPAY_KEY_SECRET', '')
razorpay_client = None
if razorpay_key_id and razorpay_key_secret:
    razorpay_client = razorpay.Client(auth=(razorpay_key_id, razorpay_key_secret))

# Delivery pricing — set in .env to enable a paid-delivery threshold.
# Defaults keep current behavior (delivery always free).
DELIVERY_FEE = float(os.environ.get('DELIVERY_FEE', '0'))
FREE_DELIVERY_THRESHOLD = float(os.environ.get('FREE_DELIVERY_THRESHOLD', '0'))
STORE_WHATSAPP = os.environ.get('STORE_WHATSAPP', '')  # e.g. 915224024567 (country code + number, digits only)

def compute_delivery_fee(subtotal: float) -> float:
    if DELIVERY_FEE <= 0:
        return 0
    if FREE_DELIVERY_THRESHOLD > 0 and subtotal >= FREE_DELIVERY_THRESHOLD:
        return 0
    return DELIVERY_FEE

def compute_discount(offer: dict, subtotal: float) -> float:
    """Compute discount from structured fields, falling back to legacy
    description parsing for offers created before discount fields existed."""
    if subtotal < offer.get('min_order', 0):
        return 0
    dtype = offer.get('discount_type')
    dvalue = offer.get('discount_value', 0)
    if dtype and dvalue > 0:
        if dtype == 'percent':
            return round(subtotal * min(dvalue, 100) / 100, 2)
        if dtype == 'flat':
            return round(min(dvalue, subtotal), 2)
        return 0
    # Legacy fallback (old offers with no discount fields)
    desc = offer.get('description', '')
    if "20%" in desc or "20 %" in desc:
        return round(subtotal * 0.20, 2)
    if "500" in desc:
        return min(500, subtotal)
    return 0

def create_token(user_id: str, email: str, role: str) -> str:
    payload = {
        'user_id': user_id,
        'email': email,
        'role': role,
        'exp': datetime.now(timezone.utc) + timedelta(days=30)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

# ==================== OTP / EMAIL HELPERS ====================
# DEV_MODE: when True, OTP and reset codes are returned in the API response and
# printed to the console instead of being sent via SMS/email. Set OTP_DEV_MODE=false
# in your .env once you've plugged in a real SMS/email provider below.
OTP_DEV_MODE = os.environ.get('OTP_DEV_MODE', 'true').lower() != 'false'
OTP_EXPIRY_MINUTES = 5
RESET_TOKEN_EXPIRY_MINUTES = 15

async def check_otp_rate_limit(key: str):
    """Allow at most 1 OTP per 60s and 5 per hour per phone/email.
    Prevents SMS-cost abuse and OTP spam."""
    now = datetime.now(timezone.utc)
    record = await db.otp_limits.find_one({"key": key}, {"_id": 0})
    timestamps = []
    if record:
        for t in record.get('timestamps', []):
            try:
                dt = datetime.fromisoformat(t)
                if now - dt < timedelta(hours=1):
                    timestamps.append(dt)
            except Exception:
                pass
    if timestamps:
        newest = max(timestamps)
        if (now - newest).total_seconds() < 60:
            raise HTTPException(status_code=429, detail="Please wait a minute before requesting another OTP")
    if len(timestamps) >= 5:
        raise HTTPException(status_code=429, detail="Too many OTP requests. Try again after an hour.")
    timestamps.append(now)
    await db.otp_limits.update_one(
        {"key": key},
        {"$set": {"key": key, "timestamps": [t.isoformat() for t in timestamps]}},
        upsert=True
    )

def generate_otp() -> str:
    """6-digit numeric OTP."""
    return f"{random.randint(0, 999999):06d}"

async def send_sms_otp(phone: str, otp: str) -> None:
    """Send an OTP over SMS.
    DEV MODE prints to console. To go live, plug your provider in below
    (e.g. MSG91 / Twilio / Fast2SMS) and set OTP_DEV_MODE=false.
    """
    if OTP_DEV_MODE:
        logger.info(f"[DEV OTP] phone={phone} otp={otp}")
        print(f"\n===== DEV OTP for {phone}: {otp} (valid {OTP_EXPIRY_MINUTES} min) =====\n")
        return
    # TODO: plug in your SMS provider here. Example (Twilio):
    #   from twilio.rest import Client
    #   client = Client(os.environ['TWILIO_SID'], os.environ['TWILIO_TOKEN'])
    #   client.messages.create(to=f"+91{phone}", from_=os.environ['TWILIO_FROM'],
    #                           body=f"Your Geeta Pujan Bhandar OTP is {otp}")
    raise HTTPException(status_code=500, detail="SMS provider not configured")

async def send_email_reset(email: str, token: str) -> None:
    """Send a password-reset code/link over email.
    DEV MODE prints to console. To go live, plug your email provider in below
    (e.g. SendGrid / Brevo / SMTP) and set OTP_DEV_MODE=false.
    """
    if OTP_DEV_MODE:
        logger.info(f"[DEV RESET] email={email} token={token}")
        print(f"\n===== DEV PASSWORD RESET for {email}: {token} (valid {RESET_TOKEN_EXPIRY_MINUTES} min) =====\n")
        return
    # TODO: plug in your email provider here. Example (SMTP):
    #   import smtplib; from email.mime.text import MIMEText
    #   msg = MIMEText(f"Your reset code is {token}")
    #   ... server.send_message(msg)
    raise HTTPException(status_code=500, detail="Email provider not configured")

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

def verify_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    payload = verify_token(credentials)
    if payload.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    return payload

class UserRegister(BaseModel):
    name: str
    email: EmailStr
    password: str
    phone: str
    otp: str   # OTP verifying the phone number, required for registration

class UserLogin(BaseModel):
    # Accepts either an email or a phone number in `identifier`.
    # `email` kept optional for backward compatibility with older clients.
    identifier: Optional[str] = None
    email: Optional[EmailStr] = None
    password: str

class OTPRequest(BaseModel):
    phone: str

class RegisterOTPRequest(BaseModel):
    name: str
    email: EmailStr
    phone: str

class OTPVerify(BaseModel):
    phone: str
    otp: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    email: EmailStr
    token: str
    new_password: str

class GoogleAuthCallback(BaseModel):
    credential: str   # Google ID token (JWT) from the frontend

class User(BaseModel):
    id: str
    name: str
    email: str
    phone: str
    role: str = "customer"
    created_at: str

class Product(BaseModel):
    id: str
    name: str
    description: str
    deity: str
    material: str
    price: float
    mrp: Optional[float] = None
    image: str
    images: List[str] = []
    stock: int
    category: str
    weight: Optional[str] = None
    variant_group: Optional[str] = None   # shared code linking size/colour variants
    size_label: Optional[str] = None       # e.g. "9 inch"
    color_label: Optional[str] = None      # e.g. "Golden"
    colors: List[dict] = []   # [{name, image}]
    sizes: List[dict] = []    # [{label, price, mrp}]
    dimensions: Optional[str] = None
    created_at: str
    avg_rating: Optional[float] = 0
    review_count: Optional[int] = 0

class ProductCreate(BaseModel):
    name: str
    description: str
    deity: str
    material: str
    price: float
    mrp: Optional[float] = None
    image: str
    images: List[str] = []
    stock: int
    category: str
    weight: Optional[str] = None
    variant_group: Optional[str] = None   # shared code linking size/colour variants
    size_label: Optional[str] = None       # e.g. "9 inch"
    color_label: Optional[str] = None      # e.g. "Golden"
    colors: List[dict] = []   # [{name, image}]
    sizes: List[dict] = []    # [{label, price, mrp}]
    dimensions: Optional[str] = None

# Banner models for promotional carousel
class BannerCreate(BaseModel):
    title: str
    # Design calls for a short descriptive line under the banner title
    # (e.g. "Light up your home with our premium brass collection.") with
    # no backend field to store it. Added end-to-end: here, on the Banner
    # model below, in the admin form, and wired into the storefront's
    # BannerCarousel display — not just admin-side decoration.
    subtitle: Optional[str] = None
    image_url: str
    target_link: str
    display_order: int = 0
    is_active: bool = True

class BannerUpdate(BaseModel):
    title: Optional[str] = None
    subtitle: Optional[str] = None
    image_url: Optional[str] = None
    target_link: Optional[str] = None
    display_order: Optional[int] = None
    is_active: Optional[bool] = None

class Banner(BaseModel):
    id: str
    title: str
    subtitle: Optional[str] = None
    image_url: str
    target_link: str
    display_order: int
    is_active: bool
    created_at: str

class Category(BaseModel):
    id: str
    name: str
    type: str
    image: str
    description: Optional[str] = None
    created_at: str

class CategoryCreate(BaseModel):
    name: str
    type: str
    image: str
    description: Optional[str] = None

class CategoryUpdate(BaseModel):
    # Previously there was no way to edit a category at all — only create
    # and delete. The new admin design has a real Edit action per card,
    # so this closes a genuine gap rather than just matching a mockup.
    name: Optional[str] = None
    type: Optional[str] = None
    image: Optional[str] = None
    description: Optional[str] = None

class Offer(BaseModel):
    id: str
    title: str
    description: str
    code: str
    image: str
    bg_color: str
    active: bool = True
    discount_type: str = "percent"  # percent | flat
    discount_value: float = 0
    min_order: float = 0
    created_at: str

class OfferCreate(BaseModel):
    title: str
    description: str
    code: str
    image: str
    bg_color: str
    active: bool = True
    discount_type: str = "percent"
    discount_value: float = 0
    min_order: float = 0

class CartItem(BaseModel):
    product_id: str
    quantity: int
    variant: Optional[str] = None   # size label, e.g. "Large"
    product: Optional[dict] = None

class Cart(BaseModel):
    user_id: str
    items: List[CartItem]

class Address(BaseModel):
    name: str
    phone: str
    address_line: str
    area: str
    pincode: str
    city: str = "Lucknow"

class Order(BaseModel):
    id: str
    user_id: str
    items: List[dict]
    total: float
    address: Address
    payment_method: str
    payment_status: str = "pending"
    order_status: str = "pending"
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    created_at: str

class OrderCreate(BaseModel):
    items: List[CartItem]
    total: float
    address: Address
    payment_method: str
    order_type: str = "delivery"   # delivery | pickup
    pickup_time: Optional[str] = None
    promo_code: Optional[str] = None
    discount: float = 0

class RazorpayOrder(BaseModel):
    # Previously: `amount: int` supplied directly by the client, trusted
    # as-is when creating the Razorpay order — meaning a shopper could
    # pay any amount they chose for any order. Amount is now always
    # derived server-side from the real order total (see
    # /payment/create-order below); the client only identifies which
    # order it's paying for.
    order_id: str
    currency: str = "INR"

class PaymentVerification(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    order_id: str

class HomepageSettings(BaseModel):
    hero_title: str
    hero_subtitle: str
    hero_image: str
    hero_description: str

# ==================== USER INTERACTION TRACKING MODELS ====================
class InteractionType(str):
    VIEW = "view"
    ADD_TO_CART = "add_to_cart"
    PURCHASE = "purchase"

class UserInteractionCreate(BaseModel):
    product_id: str
    interaction_type: str  # view, add_to_cart, purchase

class UserInteraction(BaseModel):
    id: str
    user_id: str
    product_id: str
    interaction_type: str
    product_data: dict
    created_at: str

class RecommendationResponse(BaseModel):
    recommendations: List[dict]
    is_personalized: bool
    recommendation_type: str  # "personalized", "trending", "cold_start"

# ==================== WISHLIST MODELS ====================
class Wishlist(BaseModel):
    user_id: str
    product_ids: List[str] = []

# ==================== REVIEW MODELS ====================
class ReviewCreate(BaseModel):
    rating: int
    comment: str = ""
    images: List[str] = []

class Review(BaseModel):
    id: str
    product_id: str
    user_id: str
    user_name: str
    rating: int
    comment: str
    images: List[str] = []
    created_at: str

# ==================== PINCODE / DELIVERABILITY MODELS ====================
# Lucknow-area pincodes currently served. Anything starting with one of these
# prefixes is treated as serviceable; everything else is not (store is
# Lucknow-only per the Address model default).
SERVICEABLE_PINCODE_PREFIXES = ["226"]

class PincodeCheckResponse(BaseModel):
    pincode: str
    serviceable: bool
    message: str
    estimated_days: Optional[int] = None

@api_router.post("/upload-image")
async def upload_image(file: UploadFile = File(...)):
    image_url = await upload_image_to_s3(file)

    return {
        "url": image_url
    }

@api_router.get("/homepage-settings")
async def get_homepage_settings():
    settings = await db.homepage_settings.find_one({}, {"_id": 0})
    if not settings:
        return {
            "hero_title": "Divine Collection",
            "hero_subtitle": "from Lucknow",
            "hero_image": "https://images.unsplash.com/photo-1731922910212-507d1e944dea?q=85",
            "hero_description": "Authentic religious items, handcrafted statues, and pooja essentials. Pure materials, blessed craftsmanship."
        }
    return settings

@api_router.put("/homepage-settings")
async def update_homepage_settings(settings: HomepageSettings, payload: dict = Depends(verify_admin)):
    settings_doc = settings.model_dump()
    await db.homepage_settings.replace_one({}, settings_doc, upsert=True)
    return settings_doc

@api_router.get("/")
async def root():
    return {"message": "Geeta Pujan Bhandar API"}

@api_router.get("/config")
async def get_public_config():
    """Public storefront config used by the frontend."""
    return {
        "delivery_fee": DELIVERY_FEE,
        "free_delivery_threshold": FREE_DELIVERY_THRESHOLD,
        "store_whatsapp": STORE_WHATSAPP
    }

@api_router.post("/auth/register/send-otp")
async def register_send_otp(req: RegisterOTPRequest):
    """Step 1 of registration: validate the phone/email are free, then send an OTP.
    The account is NOT created here — only after the OTP is verified via /auth/register."""
    email = req.email.strip().lower()
    phone = req.phone.strip()

    if not phone.isdigit() or len(phone) != 10:
        raise HTTPException(status_code=400, detail="Enter a valid 10-digit mobile number")

    if await db.users.find_one({"email": email}, {"_id": 0}):
        raise HTTPException(status_code=400, detail="Email already registered")
    if await db.users.find_one({"phone": phone}, {"_id": 0}):
        raise HTTPException(status_code=400, detail="Mobile number already registered")

    await check_otp_rate_limit(f"reg:{phone}")

    otp = generate_otp()
    expires = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
    await db.register_otps.update_one(
        {"phone": phone},
        {"$set": {"phone": phone, "otp": otp, "expires_at": expires.isoformat(), "attempts": 0}},
        upsert=True
    )
    await send_sms_otp(phone, otp)

    resp = {"message": "OTP sent to your mobile number"}
    if OTP_DEV_MODE:
        resp["dev_otp"] = otp
    return resp

@api_router.post("/auth/register")
async def register(user: UserRegister):
    email = user.email.strip().lower()
    phone = user.phone.strip()

    if not phone.isdigit() or len(phone) != 10:
        raise HTTPException(status_code=400, detail="Enter a valid 10-digit mobile number")

    # --- Verify the registration OTP first ---
    record = await db.register_otps.find_one({"phone": phone}, {"_id": 0})
    if not record:
        raise HTTPException(status_code=400, detail="Please verify your mobile number with an OTP first")

    if record.get('attempts', 0) >= 5:
        await db.register_otps.delete_one({"phone": phone})
        raise HTTPException(status_code=429, detail="Too many attempts. Please request a new OTP.")

    expires_at = datetime.fromisoformat(record['expires_at'])
    if datetime.now(timezone.utc) > expires_at:
        await db.register_otps.delete_one({"phone": phone})
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    if user.otp.strip() != record['otp']:
        await db.register_otps.update_one({"phone": phone}, {"$inc": {"attempts": 1}})
        raise HTTPException(status_code=400, detail="Incorrect OTP")

    # --- OTP valid: re-check uniqueness (in case someone registered meanwhile) ---
    if await db.users.find_one({"email": email}, {"_id": 0}):
        raise HTTPException(status_code=400, detail="Email already registered")
    if await db.users.find_one({"phone": phone}, {"_id": 0}):
        raise HTTPException(status_code=400, detail="Mobile number already registered")

    hashed = bcrypt.hashpw(user.password.encode('utf-8'), bcrypt.gensalt())
    user_id = str(uuid.uuid4())

    user_doc = {
        "id": user_id,
        "name": user.name,
        "email": email,
        "password": hashed.decode('utf-8'),
        "phone": phone,
        "phone_verified": True,
        "role": "customer",
        "addresses": [],
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    await db.users.insert_one(user_doc)
    await db.register_otps.delete_one({"phone": phone})
    token = create_token(user_id, email, "customer")

    return {
        "token": token,
        "user": {
            "id": user_id,
            "name": user.name,
            "email": email,
            "phone": phone,
            "role": "customer"
        }
    }

@api_router.post("/auth/login")
async def login(credentials: UserLogin):
    # Accept either an explicit identifier (email or phone) or the legacy email field
    identifier = (credentials.identifier or credentials.email or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Email or mobile number is required")

    # Decide whether the identifier is a phone (all digits, 10) or an email
    if identifier.isdigit():
        user = await db.users.find_one({"phone": identifier}, {"_id": 0})
    else:
        user = await db.users.find_one({"email": identifier.lower()}, {"_id": 0})

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.get('password'):
        raise HTTPException(status_code=400, detail="This account uses Google or OTP sign-in. Please use that method.")

    if not bcrypt.checkpw(credentials.password.encode('utf-8'), user['password'].encode('utf-8')):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_token(user['id'], user['email'], user['role'])

    return {
        "token": token,
        "user": {
            "id": user['id'],
            "name": user['name'],
            "email": user['email'],
            "phone": user['phone'],
            "role": user['role']
        }
    }

# ==================== MOBILE OTP LOGIN ====================
@api_router.post("/auth/otp/request")
async def request_otp(req: OTPRequest):
    phone = req.phone.strip()
    if not phone.isdigit() or len(phone) != 10:
        raise HTTPException(status_code=400, detail="Enter a valid 10-digit mobile number")

    user = await db.users.find_one({"phone": phone}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="No account found with this mobile number")

    await check_otp_rate_limit(f"login:{phone}")

    otp = generate_otp()
    expires = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
    # Upsert the OTP for this phone (replaces any prior unused one)
    await db.otps.update_one(
        {"phone": phone},
        {"$set": {"phone": phone, "otp": otp, "expires_at": expires.isoformat(), "attempts": 0}},
        upsert=True
    )
    await send_sms_otp(phone, otp)

    resp = {"message": "OTP sent to your mobile number"}
    if OTP_DEV_MODE:
        resp["dev_otp"] = otp  # shown on screen only in dev mode
    return resp

@api_router.post("/auth/otp/verify")
async def verify_otp(req: OTPVerify):
    phone = req.phone.strip()
    record = await db.otps.find_one({"phone": phone}, {"_id": 0})
    if not record:
        raise HTTPException(status_code=400, detail="Please request an OTP first")

    if record.get('attempts', 0) >= 5:
        await db.otps.delete_one({"phone": phone})
        raise HTTPException(status_code=429, detail="Too many attempts. Please request a new OTP.")

    expires_at = datetime.fromisoformat(record['expires_at'])
    if datetime.now(timezone.utc) > expires_at:
        await db.otps.delete_one({"phone": phone})
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    if req.otp.strip() != record['otp']:
        await db.otps.update_one({"phone": phone}, {"$inc": {"attempts": 1}})
        raise HTTPException(status_code=400, detail="Incorrect OTP")

    # Success — clean up and log the user in
    await db.otps.delete_one({"phone": phone})
    user = await db.users.find_one({"phone": phone}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="Account not found")

    token = create_token(user['id'], user['email'], user['role'])
    return {
        "token": token,
        "user": {
            "id": user['id'],
            "name": user['name'],
            "email": user['email'],
            "phone": user['phone'],
            "role": user['role']
        }
    }

# ==================== FORGOT / RESET PASSWORD ====================
@api_router.post("/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    email = req.email.strip().lower()
    user = await db.users.find_one({"email": email}, {"_id": 0})
    # Always respond the same way to avoid leaking which emails exist
    if user and user.get('password'):
        await check_otp_rate_limit(f"reset:{email}")
        token = generate_otp()  # reuse 6-digit code as reset token
        expires = datetime.now(timezone.utc) + timedelta(minutes=RESET_TOKEN_EXPIRY_MINUTES)
        await db.password_resets.update_one(
            {"email": email},
            {"$set": {"email": email, "token": token, "expires_at": expires.isoformat()}},
            upsert=True
        )
        await send_email_reset(email, token)
        resp = {"message": "If an account exists, a reset code has been sent"}
        if OTP_DEV_MODE:
            resp["dev_token"] = token
        return resp
    return {"message": "If an account exists, a reset code has been sent"}

@api_router.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest):
    email = req.email.strip().lower()
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    record = await db.password_resets.find_one({"email": email}, {"_id": 0})
    if not record or record['token'] != req.token.strip():
        raise HTTPException(status_code=400, detail="Invalid or expired reset code")

    expires_at = datetime.fromisoformat(record['expires_at'])
    if datetime.now(timezone.utc) > expires_at:
        await db.password_resets.delete_one({"email": email})
        raise HTTPException(status_code=400, detail="Reset code has expired. Please request a new one.")

    hashed = bcrypt.hashpw(req.new_password.encode('utf-8'), bcrypt.gensalt())
    await db.users.update_one({"email": email}, {"$set": {"password": hashed.decode('utf-8')}})
    await db.password_resets.delete_one({"email": email})
    return {"message": "Password reset successful. You can now log in."}

@api_router.get("/auth/me")
async def get_current_user(payload: dict = Depends(verify_token)):
    user = await db.users.find_one({"id": payload['user_id']}, {"_id": 0, "password": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

# User profile update
class UserProfileUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    gender: Optional[str] = None

class AddressCreate(BaseModel):
    name: str
    phone: str
    address_line: str
    area: str
    pincode: str
    is_default: bool = False

@api_router.put("/users/me")
async def update_user_profile(update: UserProfileUpdate, payload: dict = Depends(verify_token)):
    update_data = {k: v for k, v in update.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No data to update")
    
    await db.users.update_one(
        {"id": payload['user_id']},
        {"$set": update_data}
    )
    user = await db.users.find_one({"id": payload['user_id']}, {"_id": 0, "password": 0})
    return user

@api_router.post("/users/me/addresses")
async def add_user_address(address: AddressCreate, payload: dict = Depends(verify_token)):
    address_doc = address.model_dump()

    user = await db.users.find_one({"id": payload['user_id']}, {"_id": 0})
    has_addresses = bool(user and user.get('addresses'))

    # If this is set as default, unset other defaults (only if some exist)
    if address_doc.get('is_default') and has_addresses:
        await db.users.update_one(
            {"id": payload['user_id']},
            {"$set": {"addresses.$[].is_default": False}}
        )

    await db.users.update_one(
        {"id": payload['user_id']},
        {"$push": {"addresses": address_doc}}
    )
    return {"message": "Address added"}

@api_router.put("/users/me/addresses/{index}")
async def update_user_address(index: int, address: AddressCreate, payload: dict = Depends(verify_token)):
    user = await db.users.find_one({"id": payload['user_id']}, {"_id": 0})
    if not user or 'addresses' not in user or index >= len(user['addresses']):
        raise HTTPException(status_code=404, detail="Address not found")
    
    address_doc = address.model_dump()
    
    # If this is set as default, unset other defaults
    if address_doc.get('is_default'):
        for i, addr in enumerate(user['addresses']):
            if i != index:
                addr['is_default'] = False
        await db.users.update_one(
            {"id": payload['user_id']},
            {"$set": {"addresses": user['addresses']}}
        )
    
    await db.users.update_one(
        {"id": payload['user_id']},
        {"$set": {f"addresses.{index}": address_doc}}
    )
    return {"message": "Address updated"}

@api_router.delete("/users/me/addresses/{index}")
async def delete_user_address(index: int, payload: dict = Depends(verify_token)):
    user = await db.users.find_one({"id": payload['user_id']}, {"_id": 0})
    if not user or 'addresses' not in user or index >= len(user['addresses']):
        raise HTTPException(status_code=404, detail="Address not found")
    
    addresses = user['addresses']
    addresses.pop(index)
    
    await db.users.update_one(
        {"id": payload['user_id']},
        {"$set": {"addresses": addresses}}
    )
    return {"message": "Address deleted"}

@api_router.post("/auth/google/callback")
async def google_auth_callback(data: GoogleAuthCallback):
    """Verify a Google ID token, then create/update the user and issue a JWT."""
    try:
        idinfo = google_id_token.verify_oauth2_token(
            data.credential,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    email = idinfo.get("email")
    if not email or not idinfo.get("email_verified"):
        raise HTTPException(status_code=401, detail="Email not verified by Google")

    name = idinfo.get("name", "")
    picture = idinfo.get("picture", "")

    existing_user = await db.users.find_one({"email": email}, {"_id": 0})
    if existing_user:
        await db.users.update_one(
            {"email": email},
            {"$set": {"name": name, "picture": picture}}
        )
        user_id = existing_user["id"]
        role = existing_user.get("role", "customer")
        phone = existing_user.get("phone", "")
    else:
        user_id = str(uuid.uuid4())
        await db.users.insert_one({
            "id": user_id,
            "name": name,
            "email": email,
            "phone": "",
            "picture": picture,
            "role": "customer",
            "addresses": [],
            "auth_provider": "google",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        role, phone = "customer", ""

    token = create_token(user_id, email, role)
    return {
        "token": token,
        "user": {"id": user_id, "name": name, "email": email, "phone": phone, "role": role},
    }
async def _attach_ratings(products: List[dict]) -> List[dict]:
    """Attach avg_rating and review_count to each product dict via a single aggregation."""
    if not products:
        return products
    product_ids = [p['id'] for p in products]
    pipeline = [
        {"$match": {"product_id": {"$in": product_ids}}},
        {"$group": {"_id": "$product_id", "avg_rating": {"$avg": "$rating"}, "review_count": {"$sum": 1}}}
    ]
    rating_map = {}
    async for row in db.reviews.aggregate(pipeline):
        rating_map[row['_id']] = {
            "avg_rating": round(row['avg_rating'], 1),
            "review_count": row['review_count']
        }
    for p in products:
        info = rating_map.get(p['id'], {"avg_rating": 0, "review_count": 0})
        p['avg_rating'] = info['avg_rating']
        p['review_count'] = info['review_count']
    return products

@api_router.get("/products", response_model=List[Product])
async def get_products(deity: Optional[str] = None, material: Optional[str] = None, category: Optional[str] = None, search: Optional[str] = None, variant_group: Optional[str] = None, sort: Optional[str] = None, limit: int = 100, skip: int = 0):
    query = {}
    if deity:
        query['deity'] = deity
    if material:
        query['material'] = material
    if category:
        query['category'] = category
    if variant_group:
        query['variant_group'] = variant_group
    if search:
        # Case-insensitive search across the fields shoppers actually type
        pattern = {"$regex": re.escape(search.strip()), "$options": "i"}
        query['$or'] = [
            {"name": pattern},
            {"description": pattern},
            {"deity": pattern},
            {"material": pattern},
            {"category": pattern}
        ]

    # Sort applied at the DB level, before skip/limit, so pages stay correctly
    # ordered as the shopper paginates through "Load More" — sorting the
    # already-fetched page client-side instead would silently break on page 2+.
    # "Popularity" was in the design brief but intentionally left out: there's
    # no per-product popularity/units-sold figure stored on the product doc
    # itself (it only exists as a post-query aggregate in /admin/analytics),
    # so a correct, efficient DB-level sort for it isn't available yet.
    sort_map = {
        'newest': [('created_at', -1)],
        'price_asc': [('price', 1)],
        'price_desc': [('price', -1)],
    }
    cursor = db.products.find(query, {"_id": 0})
    if sort in sort_map:
        cursor = cursor.sort(sort_map[sort])
    products = await cursor.skip(skip).limit(limit).to_list(limit)
    products = await _attach_ratings(products)
    return products

@api_router.get("/products/{product_id}", response_model=Product)
async def get_product(product_id: str):
    product = await db.products.find_one({"id": product_id}, {"_id": 0})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    products = await _attach_ratings([product])
    return products[0]

@api_router.post("/products", response_model=Product)
async def create_product(product: ProductCreate, payload: dict = Depends(verify_admin)):
    product_id = str(uuid.uuid4())
    product_doc = product.model_dump()
    product_doc['id'] = product_id
    product_doc['created_at'] = datetime.now(timezone.utc).isoformat()
    
    await db.products.insert_one(product_doc)
    return product_doc

@api_router.put("/products/{product_id}", response_model=Product)
async def update_product(product_id: str, product: ProductCreate, payload: dict = Depends(verify_admin)):
    existing = await db.products.find_one({"id": product_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Product not found")
    
    product_doc = product.model_dump()
    product_doc['id'] = product_id
    product_doc['created_at'] = existing['created_at']
    
    await db.products.replace_one({"id": product_id}, product_doc)
    return product_doc

@api_router.delete("/products/{product_id}")
async def delete_product(product_id: str, payload: dict = Depends(verify_admin)):
    result = await db.products.delete_one({"id": product_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"message": "Product deleted"}

# ==================== BANNER ENDPOINTS ====================

@api_router.get("/banners")
async def get_active_banners():
    """Fetch all active banners sorted by display_order for customer view"""
    banners = await db.banners.find(
        {"is_active": True}, 
        {"_id": 0}
    ).sort("display_order", 1).to_list(20)
    return banners

@api_router.get("/admin/banners")
async def get_all_banners(payload: dict = Depends(verify_admin)):
    """Fetch all banners (active and inactive) for admin view"""
    banners = await db.banners.find({}, {"_id": 0}).sort("display_order", 1).to_list(100)
    return banners

@api_router.post("/admin/banners", response_model=Banner)
async def create_banner(banner: BannerCreate, payload: dict = Depends(verify_admin)):
    """Create a new promotional banner"""
    banner_id = str(uuid.uuid4())
    banner_doc = banner.model_dump()
    banner_doc['id'] = banner_id
    banner_doc['created_at'] = datetime.now(timezone.utc).isoformat()
    
    await db.banners.insert_one(banner_doc)
    banner_doc.pop('_id', None)  # Remove MongoDB's _id if present
    return banner_doc

@api_router.put("/admin/banners/{banner_id}", response_model=Banner)
async def update_banner(banner_id: str, banner: BannerUpdate, payload: dict = Depends(verify_admin)):
    """Update an existing banner"""
    existing = await db.banners.find_one({"id": banner_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Banner not found")
    
    update_data = {k: v for k, v in banner.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No data to update")
    
    await db.banners.update_one({"id": banner_id}, {"$set": update_data})
    updated = await db.banners.find_one({"id": banner_id}, {"_id": 0})
    return updated

@api_router.delete("/admin/banners/{banner_id}")
async def delete_banner(banner_id: str, payload: dict = Depends(verify_admin)):
    """Delete a banner"""
    result = await db.banners.delete_one({"id": banner_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Banner not found")
    return {"message": "Banner deleted"}

# ==================== CATEGORY ENDPOINTS ====================

@api_router.get("/categories", response_model=List[Category])
async def get_categories(type: Optional[str] = None):
    query = {}
    if type:
        query['type'] = type
    categories = await db.categories.find(query, {"_id": 0}).limit(100).to_list(100)
    return categories

@api_router.post("/categories", response_model=Category)
async def create_category(category: CategoryCreate, payload: dict = Depends(verify_admin)):
    category_id = str(uuid.uuid4())
    category_doc = category.model_dump()
    category_doc['id'] = category_id
    category_doc['created_at'] = datetime.now(timezone.utc).isoformat()
    
    await db.categories.insert_one(category_doc)
    return category_doc

@api_router.put("/categories/{category_id}", response_model=Category)
async def update_category(category_id: str, category: CategoryUpdate, payload: dict = Depends(verify_admin)):
    existing = await db.categories.find_one({"id": category_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Category not found")

    update_data = {k: v for k, v in category.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No data to update")

    await db.categories.update_one({"id": category_id}, {"$set": update_data})
    updated = await db.categories.find_one({"id": category_id}, {"_id": 0})
    return updated

@api_router.delete("/categories/{category_id}")
async def delete_category(category_id: str, payload: dict = Depends(verify_admin)):
    result = await db.categories.delete_one({"id": category_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Category not found")
    return {"message": "Category deleted"}

@api_router.get("/offers", response_model=List[Offer])
async def get_offers(active: Optional[bool] = None):
    query = {}
    if active is not None:
        query['active'] = active
    offers = await db.offers.find(query, {"_id": 0}).limit(50).to_list(50)
    return offers

@api_router.post("/offers", response_model=Offer)
async def create_offer(offer: OfferCreate, payload: dict = Depends(verify_admin)):
    offer_id = str(uuid.uuid4())
    offer_doc = offer.model_dump()
    offer_doc['id'] = offer_id
    offer_doc['created_at'] = datetime.now(timezone.utc).isoformat()
    
    await db.offers.insert_one(offer_doc)
    return offer_doc

@api_router.put("/offers/{offer_id}", response_model=Offer)
async def update_offer(offer_id: str, offer: OfferCreate, payload: dict = Depends(verify_admin)):
    existing = await db.offers.find_one({"id": offer_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Offer not found")
    
    offer_doc = offer.model_dump()
    offer_doc['id'] = offer_id
    offer_doc['created_at'] = existing['created_at']
    
    await db.offers.replace_one({"id": offer_id}, offer_doc)
    return offer_doc

@api_router.delete("/offers/{offer_id}")
async def delete_offer(offer_id: str, payload: dict = Depends(verify_admin)):
    result = await db.offers.delete_one({"id": offer_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Offer not found")
    return {"message": "Offer deleted"}

@api_router.post("/validate-promo")
async def validate_promo(code: str, total: float):
    offer = await db.offers.find_one({"code": code.upper(), "active": True}, {"_id": 0})
    if not offer:
        raise HTTPException(status_code=404, detail="Invalid or inactive promo code")

    if total < offer.get('min_order', 0):
        raise HTTPException(
            status_code=400,
            detail=f"This code needs a minimum order of ₹{int(offer['min_order'])}"
        )

    discount = compute_discount(offer, total)

    return {
        "valid": True,
        "offer": offer,
        "discount": discount,
        "message": f"Promo code '{code}' applied successfully!"
    }

@api_router.get("/cart")
async def get_cart(payload: dict = Depends(verify_token)):
    cart = await db.carts.find_one({"user_id": payload['user_id']}, {"_id": 0})
    if not cart:
        return {"user_id": payload['user_id'], "items": []}
    
    # Bulk fetch all products at once to avoid N+1 queries
    product_ids = [item['product_id'] for item in cart['items']]
    products = await db.products.find({"id": {"$in": product_ids}}, {"_id": 0}).to_list(100)
    products_dict = {p['id']: p for p in products}
    
    for item in cart['items']:
        item['product'] = products_dict.get(item['product_id'])
    
    return cart

@api_router.post("/cart")
async def add_to_cart(item: CartItem, payload: dict = Depends(verify_token)):
    cart = await db.carts.find_one({"user_id": payload['user_id']}, {"_id": 0})
    
    if not cart:
        cart = {"user_id": payload['user_id'], "items": []}
    
    existing_item = next(
        (i for i in cart['items'] if i['product_id'] == item.product_id and i.get('variant') == item.variant),
        None
    )
    if existing_item:
        existing_item['quantity'] += item.quantity
    else:
        cart['items'].append({"product_id": item.product_id, "quantity": item.quantity, "variant": item.variant})
    
    cart['updated_at'] = datetime.now(timezone.utc).isoformat()
    await db.carts.replace_one({"user_id": payload['user_id']}, cart, upsert=True)
    return {"message": "Item added to cart"}

@api_router.put("/cart/{product_id}")
async def update_cart_item(product_id: str, quantity: int, variant: Optional[str] = None, payload: dict = Depends(verify_token)):
    cart = await db.carts.find_one({"user_id": payload['user_id']}, {"_id": 0})
    if not cart:
        raise HTTPException(status_code=404, detail="Cart not found")

    def matches(i):
        return i['product_id'] == product_id and (i.get('variant') or None) == (variant or None)

    item = next((i for i in cart['items'] if matches(i)), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not in cart")

    if quantity <= 0:
        cart['items'] = [i for i in cart['items'] if not matches(i)]
    else:
        item['quantity'] = quantity
    
    await db.carts.replace_one({"user_id": payload['user_id']}, cart)
    return {"message": "Cart updated"}

@api_router.delete("/cart/{product_id}")
async def remove_from_cart(product_id: str, variant: Optional[str] = None, payload: dict = Depends(verify_token)):
    cart = await db.carts.find_one({"user_id": payload['user_id']}, {"_id": 0})
    if not cart:
        raise HTTPException(status_code=404, detail="Cart not found")

    cart['items'] = [
        i for i in cart['items']
        if not (i['product_id'] == product_id and (i.get('variant') or None) == (variant or None))
    ]
    await db.carts.replace_one({"user_id": payload['user_id']}, cart)
    return {"message": "Item removed"}

# ==================== WISHLIST ENDPOINTS ====================
@api_router.get("/wishlist")
async def get_wishlist(payload: dict = Depends(verify_token)):
    wishlist = await db.wishlists.find_one({"user_id": payload['user_id']}, {"_id": 0})
    product_ids = wishlist.get('product_ids', []) if wishlist else []
    if not product_ids:
        return {"user_id": payload['user_id'], "products": []}

    products = await db.products.find({"id": {"$in": product_ids}}, {"_id": 0}).to_list(200)
    products = await _attach_ratings(products)
    products_dict = {p['id']: p for p in products}
    # Preserve wishlist order, skip any product that no longer exists
    ordered_products = [products_dict[pid] for pid in product_ids if pid in products_dict]
    return {"user_id": payload['user_id'], "products": ordered_products}

@api_router.get("/wishlist/check/{product_id}")
async def check_in_wishlist(product_id: str, payload: dict = Depends(verify_token)):
    wishlist = await db.wishlists.find_one({"user_id": payload['user_id']}, {"_id": 0})
    in_wishlist = bool(wishlist and product_id in wishlist.get('product_ids', []))
    return {"in_wishlist": in_wishlist}

@api_router.post("/wishlist/{product_id}")
async def add_to_wishlist(product_id: str, payload: dict = Depends(verify_token)):
    product = await db.products.find_one({"id": product_id})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    wishlist = await db.wishlists.find_one({"user_id": payload['user_id']})
    if not wishlist:
        await db.wishlists.insert_one({"user_id": payload['user_id'], "product_ids": [product_id]})
    elif product_id not in wishlist.get('product_ids', []):
        await db.wishlists.update_one(
            {"user_id": payload['user_id']},
            {"$push": {"product_ids": product_id}}
        )
    return {"message": "Added to wishlist"}

@api_router.delete("/wishlist/{product_id}")
async def remove_from_wishlist(product_id: str, payload: dict = Depends(verify_token)):
    await db.wishlists.update_one(
        {"user_id": payload['user_id']},
        {"$pull": {"product_ids": product_id}}
    )
    return {"message": "Removed from wishlist"}

# ==================== REVIEW ENDPOINTS ====================
@api_router.get("/products/{product_id}/reviews")
async def get_product_reviews(product_id: str):
    reviews = await db.reviews.find({"product_id": product_id}, {"_id": 0}).sort("created_at", -1).to_list(500)
    total = len(reviews)
    average = round(sum(r['rating'] for r in reviews) / total, 1) if total > 0 else 0
    distribution = {str(i): 0 for i in range(1, 6)}
    for r in reviews:
        key = str(r['rating'])
        if key in distribution:
            distribution[key] += 1
    return {
        "reviews": reviews,
        "average_rating": average,
        "total_reviews": total,
        "distribution": distribution
    }

@api_router.post("/products/{product_id}/reviews")
async def create_review(product_id: str, review: ReviewCreate, payload: dict = Depends(verify_token)):
    if review.rating < 1 or review.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")

    product = await db.products.find_one({"id": product_id})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    existing = await db.reviews.find_one({"product_id": product_id, "user_id": payload['user_id']})
    if existing:
        raise HTTPException(status_code=400, detail="You have already reviewed this product")

    user = await db.users.find_one({"id": payload['user_id']})
    new_review = {
        "id": str(uuid.uuid4()),
        "product_id": product_id,
        "user_id": payload['user_id'],
        "user_name": user['name'] if user else "Anonymous",
        "rating": review.rating,
        "comment": review.comment.strip(),
        "images": review.images[:5],
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.reviews.insert_one(dict(new_review))
    return new_review

@api_router.delete("/products/{product_id}/reviews/{review_id}")
async def delete_review(product_id: str, review_id: str, payload: dict = Depends(verify_token)):
    review = await db.reviews.find_one({"id": review_id, "product_id": product_id})
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    if review['user_id'] != payload['user_id'] and payload.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Not authorized to delete this review")

    await db.reviews.delete_one({"id": review_id})
    return {"message": "Review deleted"}

# ==================== PINCODE / DELIVERABILITY ENDPOINT ====================
@api_router.get("/pincode-lookup/{pincode}")
async def pincode_lookup(pincode: str):
    """Look up area/city/state for a pincode via India Post's public API.
    Returns a list of areas so the user can pick the right one."""
    pincode = pincode.strip()
    if not pincode.isdigit() or len(pincode) != 6:
        raise HTTPException(status_code=400, detail="Please enter a valid 6-digit pincode")

    serviceable = any(pincode.startswith(prefix) for prefix in SERVICEABLE_PINCODE_PREFIXES)
    areas, city, state = [], "", ""
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"https://api.postalpincode.in/pincode/{pincode}")
            data = resp.json()
            if data and data[0].get("Status") == "Success":
                offices = data[0].get("PostOffice") or []
                areas = [po.get("Name", "") for po in offices if po.get("Name")]
                if offices:
                    city = offices[0].get("District", "")
                    state = offices[0].get("State", "")
    except Exception:
        # Network/API failure — return what we have; frontend falls back to manual entry
        pass

    return {"pincode": pincode, "serviceable": serviceable, "areas": areas, "city": city, "state": state}

@api_router.get("/check-pincode/{pincode}", response_model=PincodeCheckResponse)
async def check_pincode(pincode: str):
    pincode = pincode.strip()
    if not pincode.isdigit() or len(pincode) != 6:
        raise HTTPException(status_code=400, detail="Please enter a valid 6-digit pincode")

    serviceable = any(pincode.startswith(prefix) for prefix in SERVICEABLE_PINCODE_PREFIXES)
    if serviceable:
        return PincodeCheckResponse(
            pincode=pincode,
            serviceable=True,
            message="Delivery available in your area",
            estimated_days=2
        )
    return PincodeCheckResponse(
        pincode=pincode,
        serviceable=False,
        message="Sorry, we currently deliver only within Lucknow",
        estimated_days=None
    )

@api_router.post("/orders")
async def create_order(order: OrderCreate, payload: dict = Depends(verify_token)):
    order_id = str(uuid.uuid4())

    # Bulk fetch all products at once to avoid N+1 queries
    product_ids = [item.product_id for item in order.items]
    products = await db.products.find({"id": {"$in": product_ids}}, {"_id": 0}).to_list(100)
    products_dict = {p['id']: p for p in products}

    items_with_details = []
    for item in order.items:
        product = products_dict.get(item.product_id)
        if not product:
            raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")
        if item.quantity < 1:
            raise HTTPException(status_code=400, detail="Invalid quantity")
        if product.get('stock', 0) < item.quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Only {product.get('stock', 0)} left in stock for '{product['name']}'. Please update your cart."
            )
        # Resolve the unit price: if a size variant is chosen, its price wins
        unit_price = product['price']
        display_name = product['name']
        if item.variant:
            size = next((sz for sz in product.get('sizes', []) if sz.get('label') == item.variant), None)
            if size and size.get('price') is not None:
                unit_price = float(size['price'])
            display_name = f"{product['name']} ({item.variant})"
        items_with_details.append({
            "product_id": item.product_id,
            "quantity": item.quantity,
            "variant": item.variant,
            "name": display_name,
            "price": unit_price,   # price always from DB, never from client
            "image": product['image']
        })

    # --- Server-side pricing: never trust the client's total ---
    subtotal = round(sum(i['price'] * i['quantity'] for i in items_with_details), 2)
    discount = 0
    promo_code = (order.promo_code or '').strip().upper()
    if promo_code:
        offer = await db.offers.find_one({"code": promo_code, "active": True}, {"_id": 0})
        if offer:
            discount = compute_discount(offer, subtotal)
        else:
            promo_code = ''  # silently drop invalid codes rather than failing the order
    delivery_fee = 0 if order.order_type == 'pickup' else compute_delivery_fee(subtotal - discount)
    total = round(max(subtotal - discount + delivery_fee, 0), 2)

    # --- Atomically reserve stock (each update only succeeds if enough stock) ---
    decremented = []
    for item in order.items:
        result = await db.products.update_one(
            {"id": item.product_id, "stock": {"$gte": item.quantity}},
            {"$inc": {"stock": -item.quantity}}
        )
        if result.modified_count == 0:
            # Someone bought it in the meantime — roll back what we reserved
            for pid, qty in decremented:
                await db.products.update_one({"id": pid}, {"$inc": {"stock": qty}})
            name = products_dict.get(item.product_id, {}).get('name', 'item')
            raise HTTPException(status_code=409, detail=f"'{name}' just went out of stock. Please update your cart.")
        decremented.append((item.product_id, item.quantity))

    order_doc = {
        "id": order_id,
        "invoice_no": f"GPB-{datetime.now().strftime('%Y%m%d')}-{order_id[:6].upper()}",
        "user_id": payload['user_id'],
        "items": items_with_details,
        "subtotal": subtotal,
        "discount": discount,
        "promo_code": promo_code,
        "delivery_fee": delivery_fee,
        "total": total,
        "order_type": order.order_type,
        "pickup_time": order.pickup_time,
        "address": order.address.model_dump(),
        "payment_method": order.payment_method,
        "payment_status": "pending" if order.payment_method == "online" else "cod",
        "order_status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    await db.orders.insert_one(order_doc)

    # Strip MongoDB's _id (ObjectId) — FastAPI can't serialize it
    order_doc.pop("_id", None)

    # For COD, clear cart immediately.
    # For online payments, cart is cleared only after payment is verified —
    # so a failed/abandoned payment doesn't strand the user with an empty cart
    # and they won't be tempted to click Place Order again.
    if order.payment_method != "online":
        await db.carts.delete_one({"user_id": payload['user_id']})

    return order_doc

@api_router.get("/orders")
async def get_user_orders(payload: dict = Depends(verify_token), limit: int = 50, skip: int = 0):
    orders = await db.orders.find({"user_id": payload['user_id']}, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    return orders

@api_router.get("/orders/{order_id}/invoice")
async def get_order_invoice(order_id: str, payload: dict = Depends(verify_token)):
    """Generate invoice data for an order"""
    order = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    user = await db.users.find_one({"id": payload['user_id']}, {"_id": 0, "password": 0})
    
    # Prices are GST-inclusive throughout this app (see PDP's "Inclusive
    # of all taxes" and /admin/analytics' own split_gst helper, which
    # this mirrors). Tax here was previously computed as an *additional*
    # 18% on top of the item subtotal — meaning Subtotal + Tax never
    # actually equalled the real order total, which already has GST
    # baked in. Any customer who checked the invoice's own arithmetic
    # would have seen it not add up. Fixed to back-calculate the GST
    # portion already included in the total, exactly like
    # /admin/analytics already does correctly elsewhere in this file.
    subtotal = sum(item['price'] * item['quantity'] for item in order['items'])
    gst_rate = 0.18
    taxable_value = order['total'] / (1 + gst_rate)
    tax = round(order['total'] - taxable_value, 2)

    invoice_data = {
        "invoice_no": order.get('invoice_no', f"GPB-{order_id[:8].upper()}"),
        "order_id": order_id,
        "date": order['created_at'],
        "customer": {
            "name": user.get('name', order['address']['name']),
            "email": user.get('email', ''),
            "phone": order['address']['phone'],
            "address": f"{order['address']['address_line']}, {order['address']['area']}, {order['address']['pincode']}"
        },
        "items": order['items'],
        "subtotal": subtotal,
        # Previously missing entirely, despite both being stored on the
        # order — the invoice showed Subtotal -> Tax -> Total with no way
        # to see a promo discount or delivery fee that was actually applied.
        "discount": order.get('discount', 0),
        "delivery_fee": order.get('delivery_fee', 0),
        "tax": tax,
        "tax_note": "GST included in item prices, shown for reference only — not added to the total.",
        "total": order['total'],
        "payment_method": order['payment_method'],
        "payment_status": order['payment_status'],
        "company": {
            "name": "Geeta Pujan Bhandar",
            # Previously "Raja Market, Hazratganj, Lucknow - 226001" and
            # "+91 522-4024567" — a third wrong address distinct from
            # every other one found and corrected in this project.
            "address": "Latouche Road Plaza, First Floor, 92/77, Latouche Rd, Hazratganj, Lucknow \u2013 226018",
            "phone": "+91 9506711777",
            "email": "contact@geetapujan.com",
            # GSTIN is a real legal/tax identifier customers may use for
            # input tax credit claims — shipping a plausible-looking fake
            # one is worse than an obvious placeholder. Needs the actual
            # registration number from the business owner.
            "gstin": "{{GSTIN}}"
        }
    }
    
    return invoice_data

@api_router.get("/orders/{order_id}")
async def get_order(order_id: str, payload: dict = Depends(verify_token)):
    order = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order['user_id'] != payload['user_id'] and payload.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Access denied")
    
    return order

@api_router.post("/payment/create-order")
async def create_razorpay_order(order: RazorpayOrder, payload: dict = Depends(verify_token)):
    if not razorpay_client:
        raise HTTPException(status_code=503, detail="Payment service not configured. Please use Cash on Delivery.")

    # --- Amount is derived from the real order, never from the client ---
    # Previously the client sent a raw `amount` that was passed straight
    # to Razorpay with no relationship to any actual order — a shopper
    # could open dev tools and pay ₹1 for a ₹5,000 order. Now the order
    # is looked up server-side and its own `total` is the only source of
    # the amount, converted to paise (Razorpay's smallest-unit format).
    db_order = await db.orders.find_one({"id": order.order_id}, {"_id": 0})
    if not db_order:
        raise HTTPException(status_code=404, detail="Order not found")
    if db_order['user_id'] != payload['user_id']:
        raise HTTPException(status_code=403, detail="This order does not belong to you")
    if db_order['payment_status'] == 'paid':
        raise HTTPException(status_code=400, detail="This order has already been paid")

    amount_paise = round(db_order['total'] * 100)

    try:
        razorpay_order = razorpay_client.order.create({
            "amount": amount_paise,
            "currency": order.currency,
            "payment_capture": 1
        })
        # Bind this Razorpay order to our order, so /payment/verify can
        # confirm the payment being verified was actually created for
        # THIS order and not replayed from a different, cheaper one.
        await db.orders.update_one(
            {"id": order.order_id},
            {"$set": {"razorpay_order_id": razorpay_order['id']}}
        )
        return razorpay_order
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/payment/verify")
async def verify_payment(verification: PaymentVerification, payload: dict = Depends(verify_token)):
    try:
        params_dict = {
            'razorpay_order_id': verification.razorpay_order_id,
            'razorpay_payment_id': verification.razorpay_payment_id,
            'razorpay_signature': verification.razorpay_signature
        }
        razorpay_client.utility.verify_payment_signature(params_dict)
    except Exception:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    # --- Ownership + binding checks ---
    # A valid signature only proves Razorpay authorized this exact
    # razorpay_order_id/payment_id pair — it says nothing about which of
    # OUR orders the client claims it's for, since `order_id` here is
    # client-supplied. Without checking that this razorpay_order_id is
    # the one /payment/create-order actually generated for this specific
    # order, a valid signature from a small, genuinely-paid order could
    # be replayed with a different (larger) order_id to mark it paid for
    # free. Ownership is checked for the same reason /create-order checks
    # it: without it, one user could mark another user's order as paid.
    order = await db.orders.find_one({"id": verification.order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order['user_id'] != payload['user_id']:
        raise HTTPException(status_code=403, detail="This order does not belong to you")
    if order.get('razorpay_order_id') != verification.razorpay_order_id:
        raise HTTPException(status_code=400, detail="Payment does not match this order")
    if order['payment_status'] == 'paid':
        return {"status": "success", "message": "Payment already verified"}

    await db.orders.update_one(
        {"id": verification.order_id},
        {"$set": {
            "payment_status": "paid",
            "order_status": "confirmed",
            "razorpay_payment_id": verification.razorpay_payment_id
        }}
    )

    # Clear cart only after payment is confirmed
    await db.carts.delete_one({"user_id": order["user_id"]})

    return {"status": "success", "message": "Payment verified"}

@api_router.get("/admin/orders")
async def get_all_orders(status: Optional[str] = None, payload: dict = Depends(verify_admin), limit: int = 100, skip: int = 0):
    query = {}
    if status:
        query['order_status'] = status
    orders = await db.orders.find(query, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    return orders

@api_router.put("/admin/orders/{order_id}/status")
async def update_order_status(order_id: str, order_status: str, payload: dict = Depends(verify_admin)):
    order = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    await db.orders.update_one({"id": order_id}, {"$set": {"order_status": order_status}})
    if order_status == 'cancelled' and order.get('order_status') != 'cancelled':
        await restore_order_stock(order)
    return {"message": "Order status updated"}

async def restore_order_stock(order: dict):
    """Put reserved stock back exactly once per order."""
    if order.get('stock_restored'):
        return
    for item in order.get('items', []):
        await db.products.update_one(
            {"id": item['product_id']},
            {"$inc": {"stock": item.get('quantity', 0)}}
        )
    await db.orders.update_one({"id": order['id']}, {"$set": {"stock_restored": True}})

@api_router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, cancel_data: dict, payload: dict = Depends(verify_token)):
    """Allow user to cancel their own order (only if pending or confirmed)"""
    order = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order['user_id'] != payload['user_id']:
        raise HTTPException(status_code=403, detail="Not authorized to cancel this order")
    if order['order_status'] not in ['pending', 'confirmed']:
        raise HTTPException(status_code=400, detail="Order cannot be cancelled at this stage")
    
    reason = cancel_data.get('reason', 'No reason provided')
    await db.orders.update_one(
        {"id": order_id},
        {"$set": {"order_status": "cancelled", "cancel_reason": reason,
                  "cancelled_at": datetime.now(timezone.utc).isoformat()}}
    )
    await restore_order_stock(order)
    return {"message": "Order cancelled successfully"}

@api_router.get("/admin/stats")
async def get_admin_stats(payload: dict = Depends(verify_admin)):
    total_products = await db.products.count_documents({})
    total_orders = await db.orders.count_documents({})
    total_users = await db.users.count_documents({"role": "customer"})
    
    # Use aggregation for efficient stats calculation
    revenue_pipeline = [
        {"$match": {"payment_status": {"$ne": "pending"}}},
        {"$group": {"_id": None, "total_revenue": {"$sum": "$total"}}}
    ]
    revenue_result = await db.orders.aggregate(revenue_pipeline).to_list(1)
    total_revenue = revenue_result[0]['total_revenue'] if revenue_result else 0
    
    pending_orders = await db.orders.count_documents({"order_status": "pending"})
    
    return {
        "total_products": total_products,
        "total_orders": total_orders,
        "total_users": total_users,
        "total_revenue": total_revenue,
        "pending_orders": pending_orders
    }

# ==================== USER INTERACTION TRACKING & RECOMMENDATIONS ====================

@api_router.post("/tracking/interaction")
async def track_interaction(
    interaction: UserInteractionCreate,
    payload: dict = Depends(verify_token)
):
    """
    Track user interaction with a product.
    Types: view, add_to_cart, purchase
    """
    user_id = payload.get('user_id')
    
    # Get product data to store with interaction
    product = await db.products.find_one({"id": interaction.product_id}, {"_id": 0})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    interaction_doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "product_id": interaction.product_id,
        "interaction_type": interaction.interaction_type,
        "product_data": {
            "name": product.get("name"),
            "category": product.get("category"),
            "material": product.get("material"),
            "deity": product.get("deity"),
            "price": product.get("price")
        },
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.user_interactions.insert_one(interaction_doc)
    
    # Update interaction counts for popularity tracking
    await db.product_stats.update_one(
        {"product_id": interaction.product_id},
        {
            "$inc": {
                "total_interactions": 1,
                f"{interaction.interaction_type}_count": 1
            },
            "$set": {"last_interaction": datetime.now(timezone.utc).isoformat()}
        },
        upsert=True
    )
    
    return {"status": "tracked", "interaction_id": interaction_doc["id"]}

@api_router.post("/tracking/batch")
async def track_batch_interactions(
    interactions: List[UserInteractionCreate],
    payload: dict = Depends(verify_token)
):
    """
    Track multiple interactions in a single request (for efficiency).
    """
    user_id = payload.get('user_id')
    tracked = []
    
    for interaction in interactions:
        product = await db.products.find_one({"id": interaction.product_id}, {"_id": 0})
        if not product:
            continue
        
        interaction_doc = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "product_id": interaction.product_id,
            "interaction_type": interaction.interaction_type,
            "product_data": {
                "name": product.get("name"),
                "category": product.get("category"),
                "material": product.get("material"),
                "deity": product.get("deity"),
                "price": product.get("price")
            },
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        
        await db.user_interactions.insert_one(interaction_doc)
        tracked.append(interaction_doc["id"])
        
        await db.product_stats.update_one(
            {"product_id": interaction.product_id},
            {
                "$inc": {
                    "total_interactions": 1,
                    f"{interaction.interaction_type}_count": 1
                }
            },
            upsert=True
        )
    
    return {"status": "tracked", "count": len(tracked)}

@api_router.get("/recommendations")
async def get_recommendations(
    limit: int = 10,
    payload: dict = Depends(verify_token)
):
    """
    Get personalized product recommendations for the authenticated user.
    
    Uses hybrid recommendation approach:
    - Content-based filtering (similar products)
    - Collaborative filtering (users who bought X also bought Y)
    - Preference-based (category/material preferences)
    - Popularity fallback for cold start
    """
    user_id = payload.get('user_id')
    
    # Get user's interaction history
    user_interactions = await db.user_interactions.find(
        {"user_id": user_id},
        {"_id": 0}
    ).sort("created_at", -1).limit(100).to_list(100)
    
    # Get all user interactions for collaborative filtering
    all_interactions = await db.user_interactions.find(
        {},
        {"_id": 0, "user_id": 1, "product_id": 1, "interaction_type": 1}
    ).limit(10000).to_list(10000)
    
    # Get all products
    all_products = await db.products.find({}, {"_id": 0}).to_list(1000)
    
    # Get popularity stats
    stats = await db.product_stats.find({}, {"_id": 0}).to_list(1000)
    interaction_counts = {s["product_id"]: s.get("total_interactions", 0) for s in stats}
    purchase_counts = {s["product_id"]: s.get("purchase_count", 0) for s in stats}
    
    # Generate recommendations
    recommendations = recommendation_engine.get_recommendations(
        user_id=user_id,
        user_interactions=user_interactions,
        all_user_interactions=all_interactions,
        all_products=all_products,
        interaction_counts=interaction_counts,
        purchase_counts=purchase_counts,
        n=limit
    )
    
    is_personalized = len(user_interactions) > 0
    recommendation_type = "personalized" if is_personalized else "trending"
    
    return {
        "recommendations": recommendations,
        "is_personalized": is_personalized,
        "recommendation_type": recommendation_type,
        "user_interaction_count": len(user_interactions)
    }

@api_router.get("/recommendations/trending")
async def get_trending_products(limit: int = 10):
    """
    Get trending/popular products (no authentication required).
    Perfect for cold start or anonymous users.
    """
    # Get popularity stats
    stats = await db.product_stats.find({}, {"_id": 0}).sort("total_interactions", -1).limit(limit).to_list(limit)
    
    if not stats:
        # No stats yet, return latest products
        products = await db.products.find({}, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
        return {
            "products": products,
            "source": "latest"
        }
    
    # Get product details for trending products
    product_ids = [s["product_id"] for s in stats]
    products = await db.products.find(
        {"id": {"$in": product_ids}},
        {"_id": 0}
    ).to_list(limit)
    
    # Sort by interaction count
    product_map = {p["id"]: p for p in products}
    sorted_products = []
    for stat in stats:
        product = product_map.get(stat["product_id"])
        if product:
            product["trending_score"] = stat.get("total_interactions", 0)
            sorted_products.append(product)
    
    return {
        "products": sorted_products,
        "source": "trending"
    }

@api_router.get("/recommendations/similar/{product_id}")
async def get_similar_products(product_id: str, limit: int = 5):
    """
    Get products similar to a specific product.
    Useful for "You might also like" sections on product pages.
    """
    # Ensure recommendation engine is fitted
    all_products = await db.products.find({}, {"_id": 0}).to_list(1000)
    recommendation_engine.fit_products(all_products)
    
    # Get similar products
    similar_ids = recommendation_engine.get_similar_products(product_id, n=limit)
    
    if not similar_ids:
        # Fallback: return products in same category
        product = await db.products.find_one({"id": product_id}, {"_id": 0})
        if product:
            category = product.get("category")
            similar = await db.products.find(
                {"category": category, "id": {"$ne": product_id}},
                {"_id": 0}
            ).limit(limit).to_list(limit)
            return {"products": similar, "source": "category_fallback"}
        return {"products": [], "source": "none"}
    
    # Get full product data
    products = await db.products.find(
        {"id": {"$in": similar_ids}},
        {"_id": 0}
    ).to_list(limit)
    
    return {"products": products, "source": "ml_similarity"}

@api_router.get("/user/interaction-history")
async def get_user_interaction_history(
    limit: int = 50,
    payload: dict = Depends(verify_token)
):
    """
    Get user's recent interaction history.
    Useful for "Recently Viewed" sections.
    """
    user_id = payload.get('user_id')
    
    interactions = await db.user_interactions.find(
        {"user_id": user_id},
        {"_id": 0}
    ).sort("created_at", -1).limit(limit).to_list(limit)
    
    return {"interactions": interactions, "count": len(interactions)}



@api_router.get("/admin/orders/export")
async def export_orders_csv(payload: dict = Depends(verify_admin)):
    """Download all orders as a CSV for accounting."""
    from fastapi.responses import Response
    import csv, io
    orders = await db.orders.find({}, {"_id": 0}).sort("created_at", -1).to_list(20000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Invoice No", "Date", "Customer", "Phone", "Items", "Subtotal",
                     "Discount", "Delivery Fee", "Total", "Payment Method", "Payment Status", "Order Status"])
    for o in orders:
        items_str = "; ".join(f"{i.get('name','')} x{i.get('quantity',0)}" for i in o.get('items', []))
        addr = o.get('address', {})
        writer.writerow([
            o.get('invoice_no', o.get('id', '')[:8]),
            (o.get('created_at', '') or '')[:10],
            addr.get('name', ''), addr.get('phone', ''),
            items_str,
            o.get('subtotal', o.get('total', 0)),
            o.get('discount', 0), o.get('delivery_fee', 0), o.get('total', 0),
            o.get('payment_method', ''), o.get('payment_status', ''), o.get('order_status', '')
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders.csv"}
    )

@api_router.get("/admin/abandoned-carts")
async def get_abandoned_carts(hours: int = 24, payload: dict = Depends(verify_admin)):
    """Carts untouched for N hours — with customer contact for WhatsApp follow-up."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    carts = await db.carts.find({}, {"_id": 0}).to_list(5000)
    abandoned = []
    for cart in carts:
        if not cart.get('items'):
            continue
        updated = cart.get('updated_at')
        if updated:
            try:
                if datetime.fromisoformat(updated) > cutoff:
                    continue  # recently active, not abandoned
            except Exception:
                pass
        user = await db.users.find_one({"id": cart['user_id']}, {"_id": 0, "name": 1, "phone": 1, "email": 1})
        if not user:
            continue
        product_ids = [i['product_id'] for i in cart['items']]
        products = await db.products.find({"id": {"$in": product_ids}}, {"_id": 0, "id": 1, "name": 1, "price": 1}).to_list(100)
        pmap = {p['id']: p for p in products}
        value = sum(pmap.get(i['product_id'], {}).get('price', 0) * i['quantity'] for i in cart['items'])
        names = [pmap.get(i['product_id'], {}).get('name', '') for i in cart['items'] if i['product_id'] in pmap]
        abandoned.append({
            "user_name": user.get('name', ''),
            "phone": user.get('phone', ''),
            "email": user.get('email', ''),
            "items": names,
            "cart_value": round(value, 2),
            "last_updated": updated or "unknown"
        })
    abandoned.sort(key=lambda x: -x['cart_value'])
    return abandoned

@api_router.get("/admin/analytics")
async def get_admin_analytics(days: int = 30, gst_rate: float = 0.18, payload: dict = Depends(verify_admin)):
    orders = await db.orders.find({}, {"_id": 0}).to_list(20000)
    products = await db.products.find({}, {"_id": 0}).to_list(5000)
    products_dict = {p['id']: p for p in products}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    def parse_dt(val):
        try:
            dt = datetime.fromisoformat(str(val).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    # Only orders within the selected range
    range_orders = []
    for o in orders:
        dt = parse_dt(o.get('created_at'))
        if dt and dt >= cutoff:
            o['_dt'] = dt
            range_orders.append(o)

    active = [o for o in range_orders if o.get('order_status') != 'cancelled']
    cancelled = [o for o in range_orders if o.get('order_status') == 'cancelled']

    # --- Summary ---
    total_revenue = sum(o.get('total', 0) for o in active)
    total_orders = len(active)
    paid_revenue = sum(o.get('total', 0) for o in active if o.get('payment_status') == 'paid')
    units_sold = sum(i.get('quantity', 0) for o in active for i in o.get('items', []))
    summary = {
        "total_revenue": round(total_revenue, 2),
        "total_orders": total_orders,
        "avg_order_value": round(total_revenue / total_orders, 2) if total_orders else 0,
        "units_sold": units_sold,
        "paid_revenue": round(paid_revenue, 2),
        "pending_revenue": round(total_revenue - paid_revenue, 2),
        "cancelled_revenue": round(sum(o.get('total', 0) for o in cancelled), 2)
    }

    # --- Per-day revenue and orders ---
    daily_map = {}
    for i in range(days):
        day = (cutoff + timedelta(days=i + 1)).strftime('%d %b')
        daily_map[day] = {"date": day, "revenue": 0, "orders": 0}
    for o in active:
        day = o['_dt'].strftime('%d %b')
        if day in daily_map:
            daily_map[day]['revenue'] += o.get('total', 0)
            daily_map[day]['orders'] += 1
    daily = [{**d, "revenue": round(d['revenue'], 2)} for d in daily_map.values()]

    # --- Order status breakdown ---
    status_map = {}
    for o in range_orders:
        st = o.get('order_status', 'unknown')
        status_map.setdefault(st, {"status": st, "count": 0, "revenue": 0})
        status_map[st]['count'] += 1
        status_map[st]['revenue'] += o.get('total', 0)
    status_breakdown = sorted(
        [{**v, "revenue": round(v['revenue'], 2)} for v in status_map.values()],
        key=lambda x: -x['count']
    )

    # --- Monthly revenue + tax (last 6 months, all non-cancelled orders) ---
    monthly_map = {}
    for o in orders:
        if o.get('order_status') == 'cancelled':
            continue
        dt = o.get('_dt') or parse_dt(o.get('created_at'))
        if not dt or dt < now - timedelta(days=183):
            continue
        key = dt.strftime('%b %Y')
        monthly_map.setdefault(key, {"month": key, "revenue": 0, "_sort": dt.strftime('%Y-%m')})
        monthly_map[key]['revenue'] += o.get('total', 0)
    monthly = sorted(monthly_map.values(), key=lambda x: x['_sort'])
    monthly = [{"month": m['month'], "revenue": round(m['revenue'], 2)} for m in monthly]

    # --- Tax report (prices assumed GST-inclusive) ---
    def split_gst(gross):
        taxable = gross / (1 + gst_rate)
        return taxable, gross - taxable
    gross_sales = total_revenue
    taxable_value, total_gst = split_gst(gross_sales)
    tax_monthly = []
    for m in monthly:
        t, g = split_gst(m['revenue'])
        tax_monthly.append({"month": m['month'], "gross": m['revenue'], "taxable": round(t, 2), "gst": round(g, 2)})
    tax = {
        "gross_sales": round(gross_sales, 2),
        "taxable_value": round(taxable_value, 2),
        "total_gst": round(total_gst, 2),
        "cgst": round(total_gst / 2, 2),
        "sgst": round(total_gst / 2, 2),
        "monthly": tax_monthly
    }

    # --- Product performance (best sellers in range) ---
    perf = {}
    for o in active:
        for item in o.get('items', []):
            pid = item.get('product_id')
            perf.setdefault(pid, {"product_id": pid, "name": item.get('name', 'Unknown'),
                                  "image": item.get('image', ''), "units": 0, "revenue": 0, "orders": 0})
            perf[pid]['units'] += item.get('quantity', 0)
            perf[pid]['revenue'] += item.get('price', 0) * item.get('quantity', 0)
            perf[pid]['orders'] += 1
    top_products = sorted(perf.values(), key=lambda x: -x['revenue'])[:15]
    for tp in top_products:
        tp['revenue'] = round(tp['revenue'], 2)
        tp['stock'] = products_dict.get(tp['product_id'], {}).get('stock', 0)

    # --- Product report (catalog health) ---
    by_cat = {}
    for pr in products:
        cat = pr.get('category') or 'Uncategorized'
        by_cat.setdefault(cat, {"category": cat, "products": 0, "stock": 0, "stock_value": 0})
        by_cat[cat]['products'] += 1
        by_cat[cat]['stock'] += pr.get('stock', 0)
        by_cat[cat]['stock_value'] += pr.get('stock', 0) * pr.get('price', 0)
    product_report = {
        "total_products": len(products),
        "in_stock": len([p for p in products if p.get('stock', 0) > 0]),
        "low_stock": len([p for p in products if 0 < p.get('stock', 0) < 5]),
        "out_of_stock": len([p for p in products if p.get('stock', 0) == 0]),
        "by_category": sorted(
            [{**v, "stock_value": round(v['stock_value'], 2)} for v in by_cat.values()],
            key=lambda x: -x['stock_value']
        )
    }

    return {
        "summary": summary, "daily": daily, "monthly": monthly,
        "status_breakdown": status_breakdown, "top_products": top_products,
        "product_report": product_report, "tax": tax
    }



SITE_URL = os.environ.get('SITE_URL', 'http://localhost:3000').rstrip('/')

@app.get("/sitemap.xml")
async def sitemap():
    """XML sitemap for search engines: static pages + every product."""
    products = await db.products.find({}, {"_id": 0, "id": 1, "created_at": 1}).to_list(5000)
    static_pages = ["", "/products", "/return-policy"]
    urls = []
    for page in static_pages:
        urls.append(f"<url><loc>{SITE_URL}{page}</loc><changefreq>weekly</changefreq></url>")
    for prod in products:
        lastmod = str(prod.get('created_at', ''))[:10]
        lastmod_tag = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        urls.append(f"<url><loc>{SITE_URL}/products/{prod['id']}</loc>{lastmod_tag}<changefreq>weekly</changefreq></url>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(urls) + '</urlset>')
    return Response(content=xml, media_type="application/xml")

app.include_router(api_router)

from fastapi.middleware.cors import CORSMiddleware

# Set CORS_ORIGINS in .env for production, e.g.
# CORS_ORIGINS=https://geetapujan.com,https://www.geetapujan.com
cors_origins = [o.strip() for o in os.environ.get('CORS_ORIGINS', '*').split(',')]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# print("=== REGISTERED ROUTES ===")
# for route in app.routes:
#     print(route.path)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)