class VideoSumError(Exception):
    """Base application error for core domain logic."""


class LLMConfigurationError(VideoSumError):
    """Raised when LLM settings are incomplete or invalid for the current request."""


class LLMAuthenticationError(VideoSumError):
    """Raised when the upstream LLM provider rejects the supplied credentials."""


class TranscriptionConfigurationError(VideoSumError):
    """Raised when transcription provider settings are incomplete or invalid."""


class TranscriptionAuthenticationError(VideoSumError):
    """Raised when the upstream transcription provider rejects the supplied credentials."""


class UnsupportedInputError(VideoSumError):
    """Raised when an input type is not supported by the current pipeline."""


class PegasusConfigurationError(VideoSumError):
    """Raised when Twelve Labs Pegasus settings are incomplete or invalid."""


class PegasusAuthenticationError(VideoSumError):
    """Raised when Twelve Labs rejects the supplied API key."""
