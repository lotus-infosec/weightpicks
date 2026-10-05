"""Appearance (BUILD_PLAN §1.5): instance name, palette and logo (D-044).

A logo upload is PNG, JPEG or WebP, at most 512 KB. It is decoded by Pillow (with a
pixel limit against decompression bombs) and re-encoded to fresh PNGs: the original
bytes, metadata and anything hidden in the file are never stored or served. The brand
set (logo, 192 px, top-bar mark, favicon, apple-touch-icon) is written to
`/data/uploads/` and served at `/brand/<name>` instead of the project defaults.
"""

import io
import os

import structlog
from PIL import Image, UnidentifiedImageError
from sqlalchemy import Engine, select, update

from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import immediate
from app.domain import setup as setup_rules
from app.models import InstanceSettingsRow
from app.services import audit
from app.services.backups import uploads_dir

log = structlog.get_logger()

MAX_BYTES = 512 * 1024
MAX_PIXELS = 25_000_000  # e.g. 5000 x 5000; far beyond any real logo (checked per image)
FORMATS = ("PNG", "JPEG", "WEBP")
BRAND_SIZES = {
    "logo.png": 512,
    "logo-192.png": 192,
    "apple-touch-icon.png": 180,
    "mark.png": 64,
    "favicon.png": 32,
}


class AppearanceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _square(im: Image.Image) -> Image.Image:
    side = max(im.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    return canvas


def decode(data: bytes) -> Image.Image:
    """A safe, fully decoded RGBA image from untrusted bytes, or AppearanceError."""
    if not data:
        raise AppearanceError("empty", "Choose an image file.")
    if len(data) > MAX_BYTES:
        raise AppearanceError("too_big", "The logo must be 512 KB or smaller.")
    try:
        with Image.open(io.BytesIO(data), formats=list(FORMATS)) as probe:
            probe.verify()  # structure check without decoding everything
        with Image.open(io.BytesIO(data), formats=list(FORMATS)) as im:
            # Checked from the header, before any pixels are decoded (decompression bombs).
            if im.width * im.height > MAX_PIXELS:
                raise AppearanceError("too_big", "That image is too large.")
            im.load()
            decoded = im.convert("RGBA")
    except Image.DecompressionBombError as exc:
        raise AppearanceError("too_big", "That image is too large.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise AppearanceError("format", "Upload a PNG, JPEG or WebP image.") from exc
    return decoded


def save_logo(
    engine: Engine, settings: Settings, clock: Clock, actor: audit.Actor | None, data: bytes
) -> None:
    im = decode(data)
    bbox = im.getchannel("A").getbbox()
    if bbox is None:
        raise AppearanceError("blank", "That image is fully transparent.")
    tile = _square(im.crop(bbox))
    folder = uploads_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    for name, size in BRAND_SIZES.items():
        out = tile.resize((size, size), Image.Resampling.LANCZOS)
        tmp = folder / f".{name}.tmp"
        out.save(tmp, format="PNG", optimize=True)  # a fresh file: no metadata carried over
        os.replace(tmp, folder / name)
    audit.record_alone(
        engine,
        clock,
        actor,
        target=("settings", 1),
        action="appearance.logo",
        after={"bytes": len(data), "size": list(im.size)},
    )
    log.info("logo_saved", width=im.width, height=im.height)


def remove_logo(
    engine: Engine, settings: Settings, clock: Clock, actor: audit.Actor | None
) -> bool:
    folder = uploads_dir(settings)
    removed = False
    for name in BRAND_SIZES:
        path = folder / name
        if path.exists():
            path.unlink()
            removed = True
    if removed:
        audit.record_alone(
            engine, clock, actor, target=("settings", 1), action="appearance.logo_removed", after={}
        )
    return removed


def has_logo(settings: Settings) -> bool:
    return (uploads_dir(settings) / "logo.png").is_file()


def set_name_and_palette(
    engine: Engine, clock: Clock, actor: audit.Actor | None, form: dict[str, str]
) -> dict[str, str]:
    """Validate (same rules as /setup) and save; returns field errors ({} = saved)."""
    values, errors = setup_rules.appearance(form)
    if errors:
        return errors
    with immediate(engine) as conn:
        before = conn.execute(
            select(InstanceSettingsRow.app_name, InstanceSettingsRow.palette)
        ).one()
        if (before.app_name, before.palette) == (values["app_name"], values["palette"]):
            return {}
        conn.execute(
            update(InstanceSettingsRow)
            .where(InstanceSettingsRow.id == 1)
            .values(app_name=values["app_name"], palette=values["palette"], updated_at=clock.now())
        )
        audit.record(
            conn,
            clock,
            actor,
            action="settings.appearance",
            target=("settings", 1),
            before={"app_name": before.app_name, "palette": before.palette},
            after=dict(values),
        )
    return {}
