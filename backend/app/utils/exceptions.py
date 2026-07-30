"""Custom exception hierarchy for RotationEngine.

Distinguishes transient failures (retry-able) from permanent failures
(bad input, missing data) to enable smarter error handling and
appropriate HTTP status codes.
"""


class RotationEngineError(Exception):
    """Base exception for all RotationEngine errors."""
    pass


class TransientError(RotationEngineError):
    """Retry-able failures: API timeouts, rate limits, temporary outages."""
    pass


class ExternalAPIError(TransientError):
    """NBA API, DraftKings API, or AI provider failure."""

    def __init__(self, service: str, message: str):
        self.service = service
        super().__init__(f"[{service}] {message}")


class AIServiceError(TransientError):
    """AI/LLM provider timeout or quota exceeded."""

    def __init__(self, provider: str, message: str):
        self.provider = provider
        super().__init__(f"[AI/{provider}] {message}")


class DataNotFoundError(RotationEngineError):
    """Requested data does not exist (team, player, game)."""
    pass


class ValidationError(RotationEngineError):
    """Input validation failure beyond Pydantic schema checks."""
    pass


class InvalidDKFormatError(ValidationError):
    """Uploaded file is not a valid DraftKings entries CSV.

    Raised when required headers (Entry ID, Contest ID, roster slot
    columns) are missing or the file structure doesn't match the
    expected DraftKings edit-entries / template format.
    """

    def __init__(self, message: str, found_headers: list[str] | None = None):
        self.found_headers = found_headers or []
        super().__init__(message)


class ConfigurationError(RotationEngineError):
    """Missing or invalid configuration."""
    pass


class OptimizationError(RotationEngineError):
    """Lineup optimization infeasible or solver failure."""
    pass


class DataDegradationError(TransientError):
    """Pool enrichment validation failed — critical data sources missing.

    Raised when the enrichment pipeline produces a pool that fails
    quality gates (e.g. DK FPPG circuit breaker tripped, simulation
    engine timed out, AI noise profiles unavailable).  The caller
    should NOT cache the degraded pool to disk — fall back to the
    last known good file cache or retry after a cooldown.
    """

    def __init__(
        self,
        message: str,
        fppg_coverage: float = 0.0,
        sim_coverage: int = 0,
        failed_checks: list[str] | None = None,
    ):
        self.message = message
        self.fppg_coverage = fppg_coverage
        self.sim_coverage = sim_coverage
        self.failed_checks = failed_checks or []
        super().__init__(message)


class LineupGenerationError(OptimizationError):
    """Lineup generation failed after exhausting relaxation budget.

    Raised when the optimality floor constraint remains infeasible even
    after dynamic relaxation drops below the hard minimum threshold.
    """

    def __init__(
        self,
        message: str,
        baseline_score: float | None = None,
        final_threshold: float | None = None,
    ):
        self.baseline_score = baseline_score
        self.final_threshold = final_threshold
        super().__init__(message)
