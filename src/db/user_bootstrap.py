"""Multi-user bootstrap.

First-run admin creation is handled by the web setup screen. Startup only
backfills pre-existing single-user data to an admin once one exists.
"""

import logging
import os
from typing import List

from src.utils.config_loader import env_truthy

logger = logging.getLogger(__name__)


def _is_truthy_value(value: object) -> bool:
    """Whether a stored setting value reads as on.

    Per-user checkboxes post 'on', the settings form writes 'true' — both spellings
    have to count.
    """
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def reconcile_service_gates(database_service) -> List[str]:
    """One-time upgrade safety net for the install-wide service gate.

    When the global SERVICE_ENABLE_KEYS became authoritative, a service that had
    only ever been switched on per-user — with the global left at its seeded 'false'
    — would go dark for those users on upgrade. This runs exactly once, on the first
    boot after the upgrade, and switches the global on for any service where at least
    one user had it enabled.

    Contract:
    - Runs only if SERVICE_GATE_RECONCILED is not already truthy in os.environ.
    - Only acts on keys where global_service_disabled(key) is True (explicit falsey
      global). An unset global is not a decision and is never touched.
    - For each such key, counts users with that key truthy per _is_truthy_value.
    - If any user has it on, writes the global to 'true' in both DB and os.environ.
    - Persists SERVICE_GATE_RECONCILED = 'true' so the pass never runs again.
    - Never raises: on failure logs error, returns [], and leaves the marker unset
      so the next boot retries. Partial progress is safe because a gate already
      switched to 'true' no longer satisfies global_service_disabled on retry.
    """
    if env_truthy('SERVICE_GATE_RECONCILED'):
        logger.debug("Service gate reconcile: already reconciled, skipping")
        return []

    from src.utils.user_config import SERVICE_ENABLE_KEYS, global_service_disabled

    switched_on = []

    try:
        users = database_service.list_users()
        user_creds = {}
        for user in users:
            user_creds[user.id] = database_service.get_user_credentials(user.id)

        for key in sorted(SERVICE_ENABLE_KEYS):
            if not global_service_disabled(key):
                continue

            count = 0
            for creds in user_creds.values():
                if _is_truthy_value(creds.get(key)):
                    count += 1

            if count == 0:
                continue

            database_service.set_setting(key, 'true')
            os.environ[key] = 'true'
            switched_on.append(key)
            logger.info(
                "🔧 Service gate reconcile: %s was off install-wide but %d user(s) had it on "
                "— switching the global on so nobody loses sync on upgrade",
                key, count,
            )

        database_service.set_setting('SERVICE_GATE_RECONCILED', 'true')
        os.environ['SERVICE_GATE_RECONCILED'] = 'true'

        if switched_on:
            logger.info(
                "Service gate reconcile: switched on globals for %s",
                ", ".join(switched_on),
            )
        else:
            logger.debug("Service gate reconcile: no globals needed switching on")

    except Exception as e:
        logger.error("❌ Service gate reconcile failed: %s", e, exc_info=True)
        return []

    return switched_on


def _backfill_orphans_to_admin(database_service, admin) -> dict:
    counts = database_service.assign_orphan_rows_to_user(admin.id)
    moved = sum(counts.values())
    if moved:
        logger.info(
            "Multi-user bootstrap: assigned %d pre-existing rows to admin '%s' (%s)",
            moved,
            admin.username,
            ", ".join(f"{k}={v}" for k, v in counts.items() if v),
        )
    return counts


def _repair_bookorbit_links(database_service, admin) -> dict:
    """Repair legacy BookOrbit ownership links after assigning orphan rows."""
    try:
        counts = database_service.repair_missing_bookorbit_user_links()
    except Exception as exc:
        logger.warning(
            "Multi-user bootstrap: BookOrbit ownership repair failed for admin '%s': %s",
            admin.username,
            exc,
            exc_info=True,
        )
        return {}
    if counts.get("created"):
        logger.info(
            "Multi-user bootstrap: repaired %d BookOrbit ownership links for admin '%s'",
            counts["created"],
            admin.username,
        )
    return counts


def _prefill_admin_integrations_from_global(database_service, admin) -> int:
    """Seed the admin's per-user integration credentials from the existing global
    settings so an install upgrading to multi-user doesn't have to re-enter
    everything. Fills blank fields only, and considers each per-user key exactly
    once (tracked in `admin_integrations_prefilled_keys`) — so newly-promoted keys
    (e.g. ABS_COLLECTION_NAME) seed on the next startup without re-seeding or
    clobbering anything the admin already set or intentionally cleared. Returns the
    number of values copied."""
    try:
        settings = database_service.get_all_settings()
    except Exception:
        return 0

    from src.utils.user_config import PER_USER_CREDENTIAL_KEYS

    raw = settings.get("admin_integrations_prefilled_keys") or ""
    already = {k for k in raw.split(",") if k}
    # Back-compat: the old one-time boolean meant every per-user key known at that
    # time had been seeded. Seed `already` from the admin's current credentials so
    # we don't re-seed keys they already have (or set), while still picking up keys
    # newly promoted to per-user since then.
    if not already and settings.get("admin_integrations_prefilled") == "true":
        for key in PER_USER_CREDENTIAL_KEYS:
            try:
                if database_service.get_user_credential(admin.id, key):
                    already.add(key)
            except Exception:
                continue

    copied = 0
    for key in PER_USER_CREDENTIAL_KEYS:
        if key.startswith("__") or key in already:
            continue
        gval = (settings.get(key) or "").strip()
        if gval:
            try:
                if not database_service.get_user_credential(admin.id, key):
                    database_service.set_user_credential(admin.id, key, gval)
                    copied += 1
            except Exception:
                continue
        already.add(key)  # considered once — don't reconsider on future startups

    try:
        database_service.set_setting("admin_integrations_prefilled_keys", ",".join(sorted(already)))
        database_service.set_setting("admin_integrations_prefilled", "true")
    except Exception:
        pass
    if copied:
        logger.info(
            "Multi-user bootstrap: pre-filled %d global integration values into admin '%s' account",
            copied, admin.username,
        )
    return copied


def _warn_on_credential_divergence(database_service, admin) -> list:
    """Warn when an engine-mirrored credential's global settings copy differs
    from the admin's per-user account value. Background singletons (shelf
    watch, scans, ABS socket, manifest) use the global copy while syncs use
    the account copy, so silent divergence produces 'tests pass but sync or
    background features fail' reports (#328). Returns the divergent keys."""
    from src.utils.user_config import ENGINE_MIRROR_KEYS
    try:
        settings = database_service.get_all_settings()
    except Exception:
        return []
    divergent = []
    for key in ENGINE_MIRROR_KEYS:
        gval = (settings.get(key) or "").strip()
        try:
            uval = (database_service.get_user_credential(admin.id, key) or "").strip()
        except Exception:
            continue
        if uval and gval != uval:
            divergent.append(key)
            logger.warning(
                "\u26a0\ufe0f Credential divergence: global %s differs from admin '%s' account value \u2014 "
                "background services use the global copy while syncs use the account copy. "
                "Re-save Account \u2192 Integrations to reconcile.",
                key,
                admin.username,
            )
    return divergent


def bootstrap_admin_user(database_service) -> None:
    """Backfill orphan per-user rows to the first admin, if one exists.

    Idempotent and safe to run on every startup. If no users exist yet, the
    first-run setup page will create the admin and run the same backfill.
    """
    try:
        if database_service.count_users() == 0:
            logger.info("Multi-user bootstrap: no users found; first-run setup is required")
            return

        admin = next((u for u in database_service.list_users() if u.role == "admin"), None)
        if not admin:
            logger.warning("Multi-user bootstrap: no admin user found; skipping orphan backfill")
            return

        _backfill_orphans_to_admin(database_service, admin)
        _repair_bookorbit_links(database_service, admin)
        _prefill_admin_integrations_from_global(database_service, admin)
        _warn_on_credential_divergence(database_service, admin)
    except Exception as e:
        logger.error("Multi-user bootstrap failed: %s", e, exc_info=True)


def create_initial_admin_user(database_service, username: str, password: str):
    """Create the first admin user and claim pre-existing single-user rows.

    Returns (user, counts). Raises ValueError when setup is no longer allowed.
    """
    if database_service.count_users() != 0:
        raise ValueError("Initial admin already exists")
    user = database_service.create_user(username, password, role="admin")
    counts = _backfill_orphans_to_admin(database_service, user)
    _repair_bookorbit_links(database_service, user)
    _prefill_admin_integrations_from_global(database_service, user)
    return user, counts
