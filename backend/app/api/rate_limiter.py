"""Rate limiting configuration for the RotationEngine API.

Uses slowapi (a Starlette/FastAPI wrapper around limits) to enforce
per-IP request rate limits.  Stored in a separate module to avoid
circular imports between main.py and routes.py.

Default: 60 requests/minute per IP.  Individual endpoints can override
with @limiter.limit() decorators for tighter or looser limits.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60/minute"],
)
