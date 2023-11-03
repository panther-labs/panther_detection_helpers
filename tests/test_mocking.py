import unittest
from unittest.mock import MagicMock

from panther_detection_helpers import mocking

class TestMocking(unittest.TestCase):
    def test_is_mock_true(self):
        # Test using a fake mock function
        func = MagicMock(return_value="string")
        self.assertTrue(mocking.is_mock(func))

    def test_is_mock_false(self):
        # Test using a real function, like sum(x)
        self.assertFalse(mocking.is_mock(sum))