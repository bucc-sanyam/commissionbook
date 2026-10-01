import hashlib
import io
import os
import re
import tempfile
import warnings
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from flask import current_app
from PIL import Image, UnidentifiedImageError

from .validation import ValidationError


MAX_CLOUD_IMAGE = 10 * 1024 * 1024
MAX_LOCAL_IMAGE = 4 * 1024 * 1024
UPLOAD_TTL = 2 * 60 * 60
DOWNLOAD_TTL = 60
IMAGE_TYPES = {"image/png": ("PNG", ".png"), "image/jpeg": ("JPEG", ".jpg"), "image/webp": ("WEBP", ".webp")}
OBJECT_PATH = re.compile(r"evidence/[0-9a-f]{32}\.(?:png|jpg|webp)\Z")


class StorageUnavailable(RuntimeError):
    pass


def validate_image(data, content_type):
    if content_type not in IMAGE_TYPES:
        raise ValidationError("Only PNG, JPEG, and WebP images are supported.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != IMAGE_TYPES[content_type][0]:
                    raise ValidationError("The file contents do not match the declared image type.")
                if image.width * image.height > 40_000_000 or image.width > 20000 or image.height > 20000:
                    raise ValidationError("The image is too large in pixels. Resize it before uploading.")
                if getattr(image, "is_animated", False):
                    raise ValidationError("Use a still screenshot, not an animated image.")
                image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValidationError("The image is damaged or unsupported. Re-save it as PNG, JPEG, or WebP.") from exc


def safe_path(path):
    if not isinstance(path, str) or not OBJECT_PATH.fullmatch(path):
        raise ValidationError("Invalid evidence path.")
    return path


class LocalStorage:
    def __init__(self):
        self.root = Path(current_app.config["UPLOAD_DIR"])

    def store(self, path, data, content_type):
        path = safe_path(path)
        validate_image(data, content_type)
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".upload-") as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            try:
                os.link(temporary.name, destination)
            except FileExistsError:
                if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(data).digest():
                    raise ValidationError("This evidence path has already been used by another file.")

    def verify(self, asset):
        path = self.root / safe_path(asset["path"])
        try:
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise ValidationError("An image has not finished uploading. Retry the upload before submitting.") from exc
        if size != asset["byte_size"] or size > MAX_LOCAL_IMAGE:
            raise ValidationError("Uploaded image size does not match its authorization.")
        validate_image(path.read_bytes(), asset["content_type"])

    def remove(self, paths):
        for path in paths:
            (self.root / safe_path(path)).unlink(missing_ok=True)


class SupabaseStorage:
    def __init__(self):
        self.origin = current_app.config["SUPABASE_URL"]
        self.base = self.origin + "/storage/v1"
        self.bucket = current_app.config["SUPABASE_STORAGE_BUCKET"]
        key = current_app.config["SUPABASE_SERVICE_ROLE_KEY"]
        self.headers = {"Authorization": f"Bearer {key}", "apikey": key}
        self.timeout = httpx.Timeout(15, connect=5)

    def _request(self, method, path, *, payload=None, content=None, headers=None):
        try:
            response = httpx.request(
                method, self.base + path, headers={**self.headers, **(headers or {})},
                json=payload, content=content,
                timeout=self.timeout, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise StorageUnavailable("Private storage is temporarily unavailable.") from exc
        if response.status_code == 404:
            raise ValidationError("The private bucket or evidence object does not exist.")
        if not response.is_success:
            current_app.logger.error("Supabase Storage request failed with HTTP %s.", response.status_code)
            raise StorageUnavailable("Private storage rejected the request. Check the server's bucket configuration.")
        return response

    def _json(self, method, path, payload=None):
        response = self._request(method, path, payload=payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise StorageUnavailable("Private storage returned an invalid response.") from exc
        if not isinstance(data, dict):
            raise StorageUnavailable("Private storage returned an unexpected response.")
        return data

    def upload_bytes(self, path, data, content_type):
        safe_path(path)
        if len(data) > MAX_CLOUD_IMAGE:
            raise ValidationError("Evidence exceeds the 10 MB cloud file limit.")
        validate_image(data, content_type)
        self._request(
            "POST", f"/object/{self.bucket}/{quote(path, safe='/')}", content=data,
            headers={"Content-Type": content_type, "x-upsert": "false"},
        )

    def remove(self, paths):
        for path in paths:
            safe_path(path)
        if paths:
            self._request("DELETE", f"/object/{self.bucket}", payload={"prefixes": paths})

    def _signed_url(self, value):
        if not isinstance(value, str):
            raise StorageUnavailable("Private storage did not return a signed URL.")
        if value.startswith("/object/"):
            value = self.base + value
        elif value.startswith("/storage/v1/"):
            value = self.origin + value
        parsed = urlsplit(value)
        if (parsed.scheme, parsed.netloc) != (urlsplit(self.origin).scheme, urlsplit(self.origin).netloc):
            raise StorageUnavailable("Private storage returned an unexpected upload origin.")
        if not parsed.path.startswith("/storage/v1/object/") or not parsed.query:
            raise StorageUnavailable("Private storage returned an invalid signed URL.")
        return value

    def sign_upload(self, path, content_type):
        path = safe_path(path)
        result = self._json("POST", f"/object/upload/sign/{self.bucket}/{quote(path, safe='/')}", {})
        return {
            "upload_url": self._signed_url(result.get("url")),
            "method": "PUT",
            "headers": {"Content-Type": content_type, "x-upsert": "false"},
        }

    def sign_download(self, path):
        path = safe_path(path)
        result = self._json(
            "POST", f"/object/sign/{self.bucket}/{quote(path, safe='/')}",
            {"expiresIn": DOWNLOAD_TTL},
        )
        return self._signed_url(result.get("signedURL"))

    def verify(self, asset):
        path = safe_path(asset["path"])
        info = self._json("GET", f"/object/info/{self.bucket}/{quote(path, safe='/')}")
        # These top-level fields come from the stored object, not caller-supplied user metadata.
        size = info.get("size")
        mimetype = info.get("content_type")
        if str(size) != str(asset["byte_size"]) or mimetype != asset["content_type"]:
            raise ValidationError("Uploaded image size or content type does not match its authorization.")
        data = bytearray()
        try:
            with httpx.stream(
                "GET", f"{self.base}/object/authenticated/{self.bucket}/{quote(path, safe='/')}",
                headers={**self.headers, "Accept-Encoding": "identity"},
                timeout=self.timeout, follow_redirects=False,
            ) as response:
                if not response.is_success:
                    raise StorageUnavailable("The uploaded image could not be read from private storage.")
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > min(asset["byte_size"], MAX_CLOUD_IMAGE):
                        raise ValidationError("The uploaded image exceeds its authorized size.")
        except httpx.HTTPError as exc:
            raise StorageUnavailable("Private storage is temporarily unavailable.") from exc
        if len(data) != asset["byte_size"]:
            raise ValidationError("The uploaded image is incomplete.")
        validate_image(data, asset["content_type"])

    def check_bucket(self):
        bucket = self._json("GET", f"/bucket/{self.bucket}")
        if bucket.get("public") is not False:
            raise StorageUnavailable("The evidence bucket must be private.")
        limit = bucket.get("file_size_limit")
        allowed = bucket.get("allowed_mime_types")
        if not isinstance(limit, int) or limit > MAX_CLOUD_IMAGE or limit <= 0:
            raise StorageUnavailable("Set the evidence bucket file size limit to 10485760 bytes or less.")
        if not isinstance(allowed, list) or set(allowed) != set(IMAGE_TYPES):
            raise StorageUnavailable("Limit the bucket to image/png, image/jpeg, and image/webp.")
        return bucket


def get_storage():
    if current_app.config["BACKEND_MODE"] == "supabase":
        return SupabaseStorage()
    return LocalStorage()
