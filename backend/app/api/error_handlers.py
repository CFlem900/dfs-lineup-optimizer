"""Unified error response format for the RotationEngine API.

Ensures every HTTP error returns a consistent JSON structure:
    {"error": "<ErrorType>", "detail": "<message>", "status_code": <int>}
"""

import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    """Standard error payload."""

    error: str
    detail: str
    status_code: int


def register_error_handlers(app: FastAPI) -> None:
    """Attach unified error handlers to the FastAPI application."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        # Map status code to a friendly error name
        error_names = {
            400: "BadRequest",
            401: "Unauthorized",
            403: "Forbidden",
            404: "NotFound",
            405: "MethodNotAllowed",
            409: "Conflict",
            413: "PayloadTooLarge",
            422: "UnprocessableEntity",
            429: "RateLimitExceeded",
            500: "InternalServerError",
            502: "BadGateway",
            503: "ServiceUnavailable",
        }
        error_name = error_names.get(exc.status_code, "HTTPError")
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=error_name,
                detail=str(exc.detail),
                status_code=exc.status_code,
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        # Summarise validation errors into a single string
        messages = []
        for err in exc.errors():
            loc = " → ".join(str(l) for l in err.get("loc", []))
            messages.append(f"{loc}: {err.get('msg', 'invalid')}")
        detail = "; ".join(messages) if messages else str(exc)

        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="ValidationError",
                detail=detail,
                status_code=422,
            ).model_dump(),
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="BadRequest",
                detail=str(exc),
                status_code=400,
            ).model_dump(),
        )

    # ── Custom RotationEngine exception handlers ──────────────────────
    from app.utils.exceptions import (
        RotationEngineError,
        TransientError,
        DataNotFoundError,
        ValidationError as REValidationError,
        ConfigurationError,
        OptimizationError,
    )

    @app.exception_handler(DataNotFoundError)
    async def data_not_found_handler(request: Request, exc: DataNotFoundError):
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                error="NotFound",
                detail=str(exc),
                status_code=404,
            ).model_dump(),
        )

    @app.exception_handler(REValidationError)
    async def re_validation_handler(request: Request, exc: REValidationError):
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="ValidationError",
                detail=str(exc),
                status_code=400,
            ).model_dump(),
        )

    @app.exception_handler(OptimizationError)
    async def optimization_error_handler(request: Request, exc: OptimizationError):
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="OptimizationError",
                detail=str(exc),
                status_code=422,
            ).model_dump(),
        )

    @app.exception_handler(TransientError)
    async def transient_error_handler(request: Request, exc: TransientError):
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                error="ServiceUnavailable",
                detail=str(exc),
                status_code=503,
            ).model_dump(),
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(ConfigurationError)
    async def configuration_error_handler(request: Request, exc: ConfigurationError):
        logger.error(f"Configuration error: {exc}")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="ConfigurationError",
                detail="Server configuration error. Contact administrator.",
                status_code=500,
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        logger.error(
            f"Unhandled exception on {request.method} {request.url.path}: "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        )
        from app.config import get_settings
        settings = get_settings()
        detail = "An unexpected error occurred. Check server logs for details."
        if settings.environment != "production":
            detail = f"{type(exc).__name__}: {exc}"
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="InternalServerError",
                detail=detail,
                status_code=500,
            ).model_dump(),
        )
