"""Error types shared by several modules (a common vocabulary, not a module)."""


class NotAllowed(Exception):
    """The user's role does not permit the action."""


class SessionExpired(Exception):
    """The session timed out or does not exist; log in again."""
