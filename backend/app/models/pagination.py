"""Cursor-based pagination helpers.

Provides a generic ``CursorPage[T]`` envelope, ``PaginationMeta`` metadata
model, and opaque cursor encode/decode functions for keyset pagination.
"""

import base64
import json
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class PaginationMeta(BaseModel):
    """Pagination metadata included in paginated responses."""

    limit: int
    has_more: bool
    next_cursor: Optional[str] = None


class CursorPage(BaseModel, Generic[T]):
    """Generic paginated response envelope."""

    items: List[T]  # type: ignore[valid-type]
    pagination: PaginationMeta


def encode_cursor(**kwargs) -> str:
    """Encode cursor fields into an opaque base64 string."""
    return base64.urlsafe_b64encode(
        json.dumps(kwargs, default=str).encode()
    ).decode()


def decode_cursor(cursor: str) -> dict:
    """Decode an opaque cursor string back to a dict."""
    return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
