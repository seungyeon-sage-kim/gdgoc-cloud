"""File storage: QR codes and posters.

Locally, files live in .data/blobs/. On Google Cloud they live in the assets
bucket (shared/gcp.py). The services only call put/get/exists, so they never
know which one they are talking to.
"""

import mimetypes
import os
from pathlib import Path

from .config import get_settings


def _path(name: str) -> Path:
    root = get_settings().data_dir / "blobs"
    path = (root / name).resolve()
    if root.resolve() not in path.parents:
        raise ValueError("path escapes the storage root")
    return path


def put(name: str, data: bytes) -> None:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def get(name: str) -> bytes | None:
    path = _path(name)
    return path.read_bytes() if path.is_file() else None


def exists(name: str) -> bool:
    return _path(name).is_file()


def content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


# On Google Cloud, files go to the assets bucket (shared/gcp.py)
if os.environ.get("BACKEND") == "gcp":
    from .gcp import exists, put  # noqa: E402, F401, F811
    from .gcp import get_blob as get  # noqa: E402, F401, F811
