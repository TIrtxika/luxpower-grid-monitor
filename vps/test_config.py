"""
config helpers.
Run: python -m unittest test_config
"""

import os
import unittest
from unittest.mock import patch

import config


class IntEnvTest(unittest.TestCase):
    def test_empty_value_means_default(self):
        with patch.dict(os.environ, {'X_TEST_INT': ''}):
            self.assertEqual(config._int_env('X_TEST_INT', 7), 7)

    def test_value_and_missing(self):
        with patch.dict(os.environ, {'X_TEST_INT': ' 12 '}):
            self.assertEqual(config._int_env('X_TEST_INT', 7), 12)
        self.assertEqual(config._int_env('X_TEST_MISSING', 3), 3)


if __name__ == '__main__':
    unittest.main()
