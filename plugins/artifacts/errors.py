"""Domain-specific exceptions for the artifacts plugin."""


class ArtifactError(Exception):
    """Base class."""


class InvalidHtmlError(ArtifactError):
    """HTML failed validation (e.g. missing doctype)."""


class HtmlTooLargeError(ArtifactError):
    """HTML exceeds configured size cap."""


class ArtifactNotFoundError(ArtifactError):
    """No artifact row for the given id."""


class ArtifactRevokedError(ArtifactError):
    """Artifact has been revoked."""


class ArtifactExpiredError(ArtifactError):
    """Artifact passed its TTL and is not pinned."""
