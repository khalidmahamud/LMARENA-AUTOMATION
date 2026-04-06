"""Full exception hierarchy for arena automation.

Layers:
  ArenaAutomationError
  ├── ConfigError / SelectorConfigError
  ├── BrowserError
  │   ├── NavigationError
  │   ├── ChallengeDetectedError
  │   ├── RateLimitError
  │   └── SelectorNotFoundError
  ├── WorkerError
  │   ├── SubmissionError
  │   ├── ModelSelectionError
  │   ├── PollingTimeoutError
  │   └── ResponseExtractionError
  └── RunError
      ├── RunCancelledError
      └── AllWorkersFailedError
"""


class ArenaAutomationError(Exception):
    """Base exception for all arena automation errors."""


# ──── Config Layer ────


class ConfigError(ArenaAutomationError):
    """Configuration validation failure."""


class SelectorConfigError(ConfigError):
    """Selector YAML is missing or malformed."""


# ──── Browser Layer ────


class BrowserError(ArenaAutomationError):
    """Browser-level failure (launch, context creation)."""

    def __init__(self, message: str, worker_id: int = -1):
        self.worker_id = worker_id
        super().__init__(f"[Worker {worker_id}] {message}")


class NavigationError(BrowserError):
    """Failed to navigate to Arena URL."""


class ChallengeDetectedError(BrowserError):
    """Cloudflare/reCAPTCHA challenge not resolved."""

    def __init__(self, worker_id: int, challenge_type: str = "unknown"):
        self.challenge_type = challenge_type
        super().__init__(f"Challenge ({challenge_type}) not resolved", worker_id)


class RateLimitError(BrowserError):
    """Model rate limit reached on Arena."""

    def __init__(self, worker_id: int, message: str = "Rate limit reached"):
        super().__init__(message, worker_id)


class LoginDialogError(BrowserError):
    """Login dialog appeared, requiring window recreation."""

    def __init__(self, worker_id: int, message: str = "Login dialog detected"):
        super().__init__(message, worker_id)


class GenerationFailedBannerError(BrowserError):
    """Arena showed a generation-failed banner, requiring window recreation."""

    def __init__(
        self,
        worker_id: int,
        message: str = "Generation failed banner detected",
    ):
        super().__init__(message, worker_id)


class SelectorNotFoundError(BrowserError):
    """Expected DOM selector not found on page."""

    def __init__(self, selector: str, worker_id: int = -1):
        self.selector = selector
        super().__init__(f"Selector not found: {selector}", worker_id)


# ──── Worker Layer ────


class WorkerError(ArenaAutomationError):
    """Worker-level failure during Arena interaction."""

    def __init__(self, message: str, worker_id: int):
        self.worker_id = worker_id
        super().__init__(f"[Worker {worker_id}] {message}")


class SubmissionError(WorkerError):
    """Failed to paste prompt or click submit."""


class ModelSelectionError(WorkerError):
    """Failed to select the requested model."""

    def __init__(self, worker_id: int, model_name: str):
        self.model_name = model_name
        super().__init__(f"Could not select model: {model_name}", worker_id)


class PollingTimeoutError(WorkerError):
    """Response did not stabilize within timeout."""

    def __init__(self, worker_id: int, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Polling timeout after {timeout_seconds}s", worker_id)


class ResponseExtractionError(WorkerError):
    """Failed to extract response text from DOM."""


class ResponseFormatError(WorkerError):
    """Collected response did not match the requested output format."""

    def __init__(self, worker_id: int, expected_format: str, detail: str):
        self.expected_format = expected_format
        self.detail = detail
        super().__init__(
            f"Response format validation failed for {expected_format}: {detail}",
            worker_id,
        )


# ──── Orchestrator Layer ────


class RunError(ArenaAutomationError):
    """Run-level failure."""


class RunCancelledError(RunError):
    """Run was cancelled by user."""


class AllWorkersFailedError(RunError):
    """Every worker in the run failed."""

    def __init__(self, worker_count: int):
        self.worker_count = worker_count
        super().__init__(f"All {worker_count} workers failed")
