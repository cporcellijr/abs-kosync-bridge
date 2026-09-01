"""Tests for the one-time service gate reconciliation on upgrade.

When the global SERVICE_ENABLE_KEYS became authoritative, a service that had only
ever been switched on per-user — with the global left at its seeded 'false' —
would go dark for those users on upgrade. This module tests the one-time
reconciliation that runs at first boot after the upgrade, switching the global
on for any service where at least one user had it enabled.
"""

import os
import unittest

from src.db.user_bootstrap import reconcile_service_gates, _is_truthy_value
from src.utils.user_config import SERVICE_ENABLE_KEYS


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class _FakeDatabaseService:
    """Lightweight fake database service for testing reconciliation."""

    def __init__(self):
        self.settings = {}
        self.users = []
        self.credentials = {}
        self.set_calls = []

    def list_users(self):
        return self.users

    def get_user_credentials(self, user_id):
        return self.credentials.get(user_id, {})

    def set_setting(self, key, value):
        self.settings[key] = value
        self.set_calls.append((key, value))

    def get_setting(self, key):
        return self.settings.get(key)


class _EnvCase(unittest.TestCase):
    """Base class that saves/restores os.environ keys touched by tests."""

    KEYS_TO_SAVE = (
        'SERVICE_GATE_RECONCILED',
        'STORYTELLER_ENABLED',
        'ABS_ENABLED',
        'KOSYNC_ENABLED',
        'BOOKLORE_ENABLED',
        'BOOKORBIT_ENABLED',
        'KAVITA_ENABLED',
        'BOOKFUSION_ENABLED',
        'CWA_ENABLED',
        'HARDCOVER_ENABLED',
        'STORYGRAPH_ENABLED',
        'READEST_ENABLED',
        'CWA_SYNC_ENABLED',
        'READEST_ANNOTATION_SYNC',
    )

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS_TO_SAVE}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestIsTruthyValue(unittest.TestCase):
    """Test the _is_truthy_value helper."""

    def test_true_spellings(self):
        for val in ("true", "True", "TRUE", "1", "yes", "on", "on "):
            with self.subTest(val=val):
                self.assertTrue(_is_truthy_value(val))

    def test_false_spellings(self):
        for val in ("false", "False", "FALSE", "0", "no", "off", "", None):
            with self.subTest(val=val):
                self.assertFalse(_is_truthy_value(val))


class TestServiceGateReconcile(_EnvCase):
    """Tests for the reconcile_service_gates one-time upgrade pass."""

    def setUp(self):
        super().setUp()
        self.db = _FakeDatabaseService()
        self.db.users = [_FakeUser(1), _FakeUser(2)]

    def test_flips_globally_off_gate_that_user_has_on(self):
        """Global STORYTELLER_ENABLED='false', one user has 'true' -> global switched on."""
        os.environ['STORYTELLER_ENABLED'] = 'false'
        self.db.credentials = {
            1: {'STORYTELLER_ENABLED': 'true'},
            2: {},
        }

        result = reconcile_service_gates(self.db)

        self.assertEqual(result, ['STORYTELLER_ENABLED'])
        self.assertIn(('STORYTELLER_ENABLED', 'true'), self.db.set_calls)
        self.assertEqual(os.environ['STORYTELLER_ENABLED'], 'true')

    def test_accepts_on_checkbox_spelling(self):
        """User's stored value is 'on' (what the per-user form posts) -> counts as truthy."""
        os.environ['STORYTELLER_ENABLED'] = 'false'
        self.db.credentials = {
            1: {'STORYTELLER_ENABLED': 'on'},
            2: {},
        }

        result = reconcile_service_gates(self.db)

        self.assertEqual(result, ['STORYTELLER_ENABLED'])
        self.assertIn(('STORYTELLER_ENABLED', 'true'), self.db.set_calls)
        self.assertEqual(os.environ['STORYTELLER_ENABLED'], 'true')

    def test_leaves_globally_off_gate_alone_when_no_user_has_it_on(self):
        """Global false, users have it absent or 'false' -> global unchanged, marker still set."""
        os.environ['STORYTELLER_ENABLED'] = 'false'
        self.db.credentials = {
            1: {'STORYTELLER_ENABLED': 'false'},
            2: {},
        }

        result = reconcile_service_gates(self.db)

        self.assertEqual(result, [])
        # Should not call set_setting for the gate key
        gate_calls = [c for c in self.db.set_calls if c[0] == 'STORYTELLER_ENABLED']
        self.assertEqual(gate_calls, [])
        # But marker should be set
        self.assertIn(('SERVICE_GATE_RECONCILED', 'true'), self.db.set_calls)
        self.assertEqual(os.environ['SERVICE_GATE_RECONCILED'], 'true')

    def test_never_runs_twice_protects_admin_later_decision(self):
        """If marker already truthy, returns [] and calls set_setting zero times.

        This is the test that protects the admin's later decision to switch a
        service off — on the next restart the reconcile must not re-enable it.
        """
        os.environ['SERVICE_GATE_RECONCILED'] = 'true'
        os.environ['STORYTELLER_ENABLED'] = 'false'
        self.db.credentials = {
            1: {'STORYTELLER_ENABLED': 'true'},
        }

        result = reconcile_service_gates(self.db)

        self.assertEqual(result, [])
        self.assertEqual(self.db.set_calls, [])  # zero calls

    def test_sets_marker_on_clean_pass(self):
        """Marker is persisted so a subsequent call is a no-op."""
        os.environ['STORYTELLER_ENABLED'] = 'false'
        self.db.credentials = {
            1: {'STORYTELLER_ENABLED': 'true'},
        }

        result = reconcile_service_gates(self.db)

        self.assertIn(('SERVICE_GATE_RECONCILED', 'true'), self.db.set_calls)
        self.assertEqual(os.environ['SERVICE_GATE_RECONCILED'], 'true')

        # Second call should be no-op
        self.db.set_calls.clear()
        result2 = reconcile_service_gates(self.db)
        self.assertEqual(result2, [])
        self.assertEqual(self.db.set_calls, [])

    def test_unset_global_is_not_a_decision(self):
        """Delete the gate key from os.environ entirely; user has it 'true' -> do not write."""
        os.environ.pop('STORYTELLER_ENABLED', None)
        self.db.credentials = {
            1: {'STORYTELLER_ENABLED': 'true'},
        }

        result = reconcile_service_gates(self.db)

        # global_service_disabled only treats explicit falsey as off
        self.assertEqual(result, [])
        gate_calls = [c for c in self.db.set_calls if c[0] == 'STORYTELLER_ENABLED']
        self.assertEqual(gate_calls, [])
        # Marker still set
        self.assertIn(('SERVICE_GATE_RECONCILED', 'true'), self.db.set_calls)

    def test_db_failure_does_not_set_marker_and_does_not_raise(self):
        """Fake's set_setting raises on the gate key -> returns [], does not propagate,
        leaves SERVICE_GATE_RECONCILED unset so next boot retries."""
        os.environ['STORYTELLER_ENABLED'] = 'false'
        self.db.credentials = {
            1: {'STORYTELLER_ENABLED': 'true'},
        }

        def failing_set(key, value):
            if key == 'STORYTELLER_ENABLED':
                raise RuntimeError("DB write failed")
            _FakeDatabaseService.set_setting(self.db, key, value)

        self.db.set_setting = failing_set

        result = reconcile_service_gates(self.db)

        self.assertEqual(result, [])
        # Marker must not be set so retry happens on next boot
        self.assertNotIn(('SERVICE_GATE_RECONCILED', 'true'), self.db.set_calls)
        self.assertFalse(os.environ.get('SERVICE_GATE_RECONCILED', '').strip().lower() in ('true', '1', 'yes', 'on'))

    def test_only_keys_in_service_enable_keys_are_considered(self):
        """Non-gate per-user key (e.g. READEST_ANNOTATION_SYNC) with global 'false'
        and user 'true' is not switched on — feature sub-toggles are deliberately
        outside the gate."""
        os.environ['READEST_ANNOTATION_SYNC'] = 'false'
        self.assertNotIn('READEST_ANNOTATION_SYNC', SERVICE_ENABLE_KEYS)
        self.db.credentials = {
            1: {'READEST_ANNOTATION_SYNC': 'true'},
        }

        result = reconcile_service_gates(self.db)

        # Should not touch READEST_ANNOTATION_SYNC
        gate_calls = [c for c in self.db.set_calls if c[0] == 'READEST_ANNOTATION_SYNC']
        self.assertEqual(gate_calls, [])
        # Marker still set
        self.assertIn(('SERVICE_GATE_RECONCILED', 'true'), self.db.set_calls)


if __name__ == '__main__':
    unittest.main()