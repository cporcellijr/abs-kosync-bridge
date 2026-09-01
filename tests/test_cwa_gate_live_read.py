"""Tests that CWA enable flags are read live per call, not snapshotted at construction.

These tests assert that an instance constructed while the flag is ON will see it
turn OFF later, and vice versa, without any restart or re-instantiation.
"""

import os
import unittest

from src.api.cwa_client import CWAClient
from src.api.cwa_sync_api import CWASyncApi


class CWAClientLiveReadTest(unittest.TestCase):
    """Test CWAClient sees global enable flag changes without re-construction."""

    _env_keys = (
        "CWA_SERVER",
        "CWA_ENABLED",
        "CWA_SYNC_ENABLED",
        "CWA_SYNC_TOKEN",
        "CWA_USERNAME",
        "CWA_PASSWORD",
    )

    def setUp(self):
        self._original = {}
        for k in self._env_keys:
            self._original[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_client_sees_later_global_switch_off(self):
        """CWAClient constructed with CWA_ENABLED=true returns False after env changes to false."""
        os.environ["CWA_SERVER"] = "http://cwa.test"
        os.environ["CWA_ENABLED"] = "true"

        client = CWAClient()

        self.assertTrue(client.is_configured())

        os.environ["CWA_ENABLED"] = "false"

        self.assertFalse(client.is_configured())

    def test_client_sees_later_global_switch_on(self):
        """CWAClient constructed with CWA_ENABLED=false returns True after env changes to true."""
        os.environ["CWA_SERVER"] = "http://cwa.test"
        os.environ["CWA_ENABLED"] = "false"

        client = CWAClient()

        self.assertFalse(client.is_configured())

        os.environ["CWA_ENABLED"] = "true"

        self.assertTrue(client.is_configured())

    def test_per_user_credentials_win_when_global_unset(self):
        """Per-user CWA_ENABLED=true makes is_configured True even when global is unset."""
        # Global unset, not false
        os.environ.pop("CWA_ENABLED", None)
        os.environ["CWA_SERVER"] = "http://cwa.global"

        credentials = {
            "CWA_ENABLED": "true",
            "CWA_SERVER": "http://cwa.user",
        }
        client = CWAClient(credentials=credentials)

        self.assertTrue(client.is_configured())


class CWASyncApiLiveReadTest(unittest.TestCase):
    """Test CWASyncApi sees global sync enable flag changes without re-construction."""

    _env_keys = (
        "CWA_SERVER",
        "CWA_ENABLED",
        "CWA_SYNC_ENABLED",
        "CWA_SYNC_TOKEN",
        "CWA_USERNAME",
        "CWA_PASSWORD",
    )

    def setUp(self):
        self._original = {}
        for k in self._env_keys:
            self._original[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_sync_api_sees_later_global_switch_off(self):
        """CWASyncApi constructed with CWA_SYNC_ENABLED=true returns False after env changes to false."""
        os.environ["CWA_SERVER"] = "http://cwa.test"
        os.environ["CWA_SYNC_TOKEN"] = "tok123"
        os.environ["CWA_SYNC_ENABLED"] = "true"

        api = CWASyncApi(cwa_client=None)

        self.assertTrue(api.is_configured())

        os.environ["CWA_SYNC_ENABLED"] = "false"

        self.assertFalse(api.is_configured())


if __name__ == "__main__":
    unittest.main()