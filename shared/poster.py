"""Event poster: resize an upload into the two images the site uses.

  web.jpg  up to 1600 px wide, on the event page and its card
  og.jpg   1200x630 crop for link previews (KakaoTalk, Discord)

The caller stores them as posters/{event id}/{name}.

This runs inside web when an organizer uploads: a few hundred milliseconds of
work, once per event, does not need a service of its own.
"""

import io

from PIL import Image, ImageOps

WEB_WIDTH = 1600
PREVIEW_SIZE = (1200, 630)


def _jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=85, optimize=True, progressive=True)
    return out.getvalue()


def render(data: bytes) -> dict:
    """Returns {name: jpeg bytes}. Raises ValueError for files that are not images."""
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    except Exception as exc:
        raise ValueError("not an image") from exc
    web = image.copy()
    web.thumbnail((WEB_WIDTH, WEB_WIDTH * 2))
    preview = ImageOps.fit(image, PREVIEW_SIZE, method=Image.Resampling.LANCZOS)
    return {"web.jpg": _jpeg(web), "og.jpg": _jpeg(preview)}
