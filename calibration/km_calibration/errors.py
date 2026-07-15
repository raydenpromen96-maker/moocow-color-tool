class CalibrationError(Exception):
    """Base error for calibration operations that must fail closed."""


class DatasetValidationError(CalibrationError):
    """Raised when a calibration dataset violates the immutable v1 contract."""


class IdentifiabilityError(CalibrationError):
    """Raised when the data cannot recover transferable mm^-1 K and S curves."""
