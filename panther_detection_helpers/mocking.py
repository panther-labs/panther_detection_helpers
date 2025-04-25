from typing import Callable
from unittest.mock import MagicMock


def is_mock(func: Callable) -> bool:
    """Checks if a callable 'func' is a Panther mock function or not."""
    # We may one day use a different class for mocking, so to prevent customers from banking on the
    # idea what we use MagicMock as a backend, we're performing this check in the private code of
    # panther_detection_helpers, and not publicly in panther_analysis.
    return isinstance(func, MagicMock)
