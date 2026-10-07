"""Application errors rendered as HTML, never as stack traces."""


class AppError(Exception):
    """A request failed in a way the user can act on."""

    def __init__(self, status_code: int, heading: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.heading = heading
        self.message = message


class OrderError(Exception):
    """Checkout could not be completed. The message is a fixed sentence."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
