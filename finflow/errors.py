class ApiError(Exception):
    """Raised anywhere in a request; the app turns it into a JSON error response."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message
