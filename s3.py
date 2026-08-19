import boto3
from botocore.exceptions import ClientError
import uuid
import io
from dotenv import load_dotenv
import os

try:
    from PIL import Image, ImageOps
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

load_dotenv('.env')
aws_access_key_id = os.getenv('aws_access_key_id')
aws_secret_access_key = os.getenv('aws_secret_access_key')
s3_client = boto3.client(
    "s3",
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
    region_name="ap-south-1"
)

BUCKET_NAME = "geetapujanbhandar"

MAX_DIMENSION = 1600   # px — plenty for product zoom, keeps files small
JPEG_QUALITY = 82

def _compress_image(raw: bytes, content_type: str):
    """Resize/compress images before upload so pages load fast.
    Returns (bytes, content_type, extension) — original if compression not possible."""
    if not PIL_AVAILABLE or content_type in ("image/gif", "image/svg+xml"):
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)  # respect phone-camera rotation
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
        out = io.BytesIO()
        if content_type == "image/png" and img.mode in ("RGBA", "LA", "P"):
            img.save(out, format="PNG", optimize=True)
            return out.getvalue(), "image/png", "png"
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue(), "image/jpeg", "jpg"
    except Exception:
        return None

async def upload_image_to_s3(file):
    raw = await file.read()
    content_type = file.content_type or "application/octet-stream"
    extension = file.filename.split(".")[-1].lower() if "." in file.filename else "bin"

    compressed = _compress_image(raw, content_type)
    if compressed:
        raw, content_type, extension = compressed

    filename = f"products/{uuid.uuid4()}.{extension}"
    s3_client.upload_fileobj(
        io.BytesIO(raw),
        BUCKET_NAME,
        filename,
        ExtraArgs={"ContentType": content_type}
    )
    return f"https://{BUCKET_NAME}.s3.amazonaws.com/{filename}"