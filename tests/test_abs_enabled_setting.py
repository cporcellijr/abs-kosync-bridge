"""
Audiobookshelf finally has an on/off switch.

Every other integration ships an ``X_ENABLED`` flag; ABS had none, so the only
way to turn it off was typing the literal word ``disabled`` into the server URL —
a global field. A user who stopped using ABS while another user on the same
install still relied on it had nowhere to say so.

``ABS_ENABLED`` fills that gap, global and per-user. Its default is ``'true'``,
unlike every other service, because ``bootstrap_config()`` reconciles new keys
into existing installs: a ``'false'`` default would silently switch ABS off for
everyone on upgrade. Blank means enabled for the same reason — a regular user's
per-user lookup returns the default, not the global value.
"""

import os
import unittest

from src.api.api_clients import ABSClient
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
from src.utils.user_config import (
    ENGINE_MIRROR_KEYS,
    PER_USER_CREDENTIAL_KEYS,
    PER_USER_FIELD_GROUPS,
)


class TestAbsEnabledRegistration(unittest.TestCase):
    """The setting exists everywhere the loader and the per-user page look."""

    def test_registered_with_an_enabled_default(self):
        self.assertIn('ABS_ENABLED', ALL_SETTINGS)
        self.assertEqual(DEFAULT_CONFIG['ABS_ENABLED'], 'true')

    def test_declared_as_a_per_user_toggle(self):
        self.assertIn('ABS_ENABLED', PER_USER_CREDENTIAL_KEYS)
        groups = dict(PER_USER_FIELD_GROUPS)
        self.assertIn(('ABS_ENABLED', 'Enabled', 'bool'), groups['Audiobookshelf'])

    def test_not_mirrored_to_the_global_config(self):
        """The primary admin's per-user values are mirrored outward to the global
        settings the engine singletons authenticate with. An off switch that
        mirrored would take ABS down for every other user too — the exact problem
        this setting exists to avoid."""
        self.assertNotIn('ABS_ENABLED', ENGINE_MIRROR_KEYS)


class TestAbsEnabledGatesTheClient(unittest.TestCase):
    """``ABSClient.is_configured`` is the choke point every ABS path runs through."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ('ABS_SERVER', 'ABS_KEY', 'ABS_ENABLED')}
        os.environ['ABS_SERVER'] = 'http://abs.test'
        os.environ['ABS_KEY'] = 'token-123'
        os.environ.pop('ABS_ENABLED', None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_absent_setting_leaves_abs_enabled(self):
        """Upgrade path: nothing in the environment yet, ABS keeps working."""
        self.assertTrue(ABSClient().is_configured())

    def test_truthy_spellings_enable(self):
        for value in ('true', 'on', '1', 'yes', 'TRUE'):
            with self.subTest(value=value):
                os.environ['ABS_ENABLED'] = value
                self.assertTrue(ABSClient().is_configured())

    def test_falsey_spellings_disable(self):
        for value in ('false', 'off', '0', 'no', 'FALSE'):
            with self.subTest(value=value):
                os.environ['ABS_ENABLED'] = value
                self.assertFalse(ABSClient().is_configured())

    def test_per_user_off_switch_beats_an_enabled_global(self):
        """The reported case: one user drops ABS, the install keeps it."""
        os.environ['ABS_ENABLED'] = 'true'
        credentials = {'ABS_ENABLED': 'false', 'ABS_KEY': 'user-token'}
        self.assertFalse(ABSClient(credentials=credentials).is_configured())

    def test_other_users_keep_syncing(self):
        """A user who never touched the toggle is unaffected, including a regular
        user whose blank per-user keys deliberately do not inherit the global."""
        os.environ['ABS_ENABLED'] = 'true'
        self.assertTrue(ABSClient(credentials={'ABS_KEY': 'user-token'}).is_configured())
        no_fallback = {'ABS_KEY': 'user-token', '__allow_global_fallback__': False}
        self.assertTrue(ABSClient(credentials=no_fallback).is_configured())

    def test_disabled_sentinel_still_works(self):
        """The legacy compose-env off switch is untouched."""
        os.environ['ABS_ENABLED'] = 'true'
        os.environ['ABS_SERVER'] = 'disabled'
        self.assertFalse(ABSClient().is_configured())


if __name__ == '__main__':
    unittest.main()
