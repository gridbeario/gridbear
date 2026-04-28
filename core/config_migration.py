"""One-time migration: admin_config.json -> PostgreSQL ORM models.

Follows the same idempotent pattern as the plugins.json migration in
``core/plugin_registry/registry.py``:
  1. Check SystemConfig marker — if set, skip
  2. Read JSON file
  3. INSERT into ORM models
  4. Set marker
  5. Rename file to ``.migrated``
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_MARKER_KEY = "_migration_admin_config"
_MARKER_REST_API_KEY = "_migration_rest_api_config"
_MARKER_CLAUDE_SETTINGS_KEY = "_migration_claude_settings"
_MARKER_MCP_PERMS_UNIFIED_ID = "_migration_mcp_perms_unified_id"
_MARKER_DEFAULT_COMPANY = "_migration_default_company"
_MARKER_UNIFY_USERS = "_migration_unify_users"
_MARKER_USER_PLATFORMS = "_migration_user_platforms"
_MARKER_SEED_LANGUAGES = "_migration_seed_languages"
_MARKER_RENAME_THEME_ENTERPRISE = "_migration_rename_theme_enterprise_to_corporate"


async def migrate_admin_config_to_db(config_path: Path) -> bool:
    """Migrate admin_config.json data into PostgreSQL tables.

    Returns True if migration was performed, False if skipped.
    """
    from core.config_models import (
        ChannelAuthorizedUser,
        GroupMcpPermission,
        MemoryGroup,
        OAuthToken,
        UserMcpPermission,
        UserServiceAccount,
    )
    from core.registry import get_database
    from core.system_config import SystemConfig

    # 1. Check marker
    marker = await SystemConfig.get_param(_MARKER_KEY)
    if marker:
        return False

    # 2. Read file
    if not config_path.exists():
        logger.info("No admin_config.json found — marking migration as done")
        await SystemConfig.set_param(_MARKER_KEY, True)
        return False

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, IOError) as exc:
        logger.error("Failed to read admin_config.json: %s", exc)
        return False

    logger.info("Migrating admin_config.json to PostgreSQL...")

    # 3. Migrate each section

    # --- Channel authorized users ---
    for key, data in config.items():
        if not key.endswith("_authorized"):
            continue
        channel = key.removesuffix("_authorized")
        for uid in data.get("ids", []):
            try:
                await ChannelAuthorizedUser.create(
                    channel=channel, platform_user_id=uid
                )
            except Exception as exc:
                logger.debug("Channel user %s/%s skip: %s", channel, uid, exc)
        for uname in data.get("usernames", []):
            try:
                await ChannelAuthorizedUser.create(
                    channel=channel, username=uname.lower()
                )
            except Exception as exc:
                logger.debug("Channel user %s/%s skip: %s", channel, uname, exc)

    # --- User identities → user_platforms (via raw SQL) ---
    db = get_database()
    async with db.acquire() as conn:
        for unified_id, platforms in config.get("user_identities", {}).items():
            unified_id = unified_id.lower()
            for platform, username in platforms.items():
                try:
                    await conn.execute(
                        """
                        INSERT INTO app.user_platforms (user_id, platform, platform_username)
                        SELECT u.id, %s, %s
                        FROM app.users u WHERE LOWER(u.username) = %s
                        ON CONFLICT DO NOTHING
                        """,
                        (platform.lower(), username.lower().lstrip("@"), unified_id),
                    )
                except Exception as exc:
                    logger.debug("Identity %s/%s skip: %s", unified_id, platform, exc)

    # --- User MCP permissions (stored by unified_id) ---
    # Legacy JSON used usernames; resolve to unified_id via identity mapping
    identity_map = {}
    for uid, platforms in config.get("user_identities", {}).items():
        for _platform, uname in platforms.items():
            identity_map[uname.lower().lstrip("@")] = uid.lower()

    for username, servers in config.get("user_permissions", {}).items():
        username_lower = username.lower().lstrip("@")
        uid = identity_map.get(username_lower, username_lower)
        for server in servers:
            try:
                await UserMcpPermission.create(unified_id=uid, server_name=server)
            except Exception as exc:
                logger.debug("MCP perm %s/%s skip: %s", uid, server, exc)

    # --- Memory groups ---
    for group_name, members in config.get("memory_groups", {}).items():
        group_name = group_name.lower()
        for member in members:
            try:
                await MemoryGroup.create(
                    group_name=group_name, unified_id=member.lower()
                )
            except Exception as exc:
                logger.debug("Memory group %s/%s skip: %s", group_name, member, exc)

    # --- Group permissions ---
    for group_name, servers in config.get("group_permissions", {}).items():
        group_name = group_name.lower()
        for server in servers:
            try:
                await GroupMcpPermission.create(
                    group_name=group_name, server_name=server
                )
            except Exception as exc:
                logger.debug("Group perm %s/%s skip: %s", group_name, server, exc)

    # --- Gmail accounts -> UserServiceAccount ---
    for unified_id, emails in config.get("gmail_accounts", {}).items():
        unified_id = unified_id.lower()
        if isinstance(emails, str):
            emails = [emails]
        for email in emails:
            try:
                await UserServiceAccount.create(
                    unified_id=unified_id,
                    service_type="gmail",
                    account_id=email,
                )
            except Exception as exc:
                logger.debug("Service acct %s/%s skip: %s", unified_id, email, exc)

    # --- User locales → User.locale (direct update) ---
    async with db.acquire() as conn:
        for unified_id, locale in config.get("user_locales", {}).items():
            try:
                await conn.execute(
                    "UPDATE app.users SET locale = %s WHERE LOWER(username) = %s",
                    (locale.lower(), unified_id.lower()),
                )
            except Exception as exc:
                logger.debug("User locale %s skip: %s", unified_id, exc)

    # --- OAuth tokens (ephemeral, but migrate any still valid) ---
    for token, data in config.get("oauth_tokens", {}).items():
        try:
            await OAuthToken.create(
                token=token, unified_id=data.get("unified_id", "").lower()
            )
        except Exception as exc:
            logger.debug("OAuth token skip: %s", exc)

    # --- Global settings -> SystemConfig ---
    bot_identity = config.get("bot_identity")
    if bot_identity:
        await SystemConfig.set_param("bot_identity", bot_identity)

    bot_email = config.get("bot_email_settings")
    if bot_email:
        await SystemConfig.set_param("bot_email_settings", bot_email)

    tts = config.get("webchat_tts_provider")
    if tts:
        await SystemConfig.set_param("webchat_tts_provider", tts)

    # 4. Mark migration as done
    await SystemConfig.set_param(_MARKER_KEY, True)

    # 5. Rename file
    migrated_path = config_path.with_suffix(".json.migrated")
    try:
        config_path.rename(migrated_path)
        logger.info("Renamed admin_config.json -> admin_config.json.migrated")
    except OSError as exc:
        logger.warning("Could not rename admin_config.json: %s", exc)

    logger.info("Migrated admin_config.json to PostgreSQL successfully")
    return True


async def migrate_rest_api_config_to_db(config_path: Path) -> bool:
    """Migrate rest_api.json data into PostgreSQL SystemConfig.

    Returns True if migration was performed, False if skipped.
    """
    from core.system_config import SystemConfig

    # 1. Check marker
    marker = await SystemConfig.get_param(_MARKER_REST_API_KEY)
    if marker:
        return False

    # 2. Read file
    if not config_path.exists():
        logger.info("No rest_api.json found — marking migration as done")
        await SystemConfig.set_param(_MARKER_REST_API_KEY, True)
        return False

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, IOError) as exc:
        logger.error("Failed to read rest_api.json: %s", exc)
        return False

    logger.info("Migrating rest_api.json to PostgreSQL...")

    # 3. Store as single SystemConfig parameter
    await SystemConfig.set_param("rest_api_config", config)

    # 4. Mark migration as done
    await SystemConfig.set_param(_MARKER_REST_API_KEY, True)

    # 5. Rename file
    migrated_path = config_path.with_suffix(".json.migrated")
    try:
        config_path.rename(migrated_path)
        logger.info("Renamed rest_api.json -> rest_api.json.migrated")
    except OSError as exc:
        logger.warning("Could not rename rest_api.json: %s", exc)

    logger.info("Migrated rest_api.json to PostgreSQL successfully")
    return True


async def migrate_claude_settings_to_db(config_path: Path) -> bool:
    """Migrate claude_settings.json data into PostgreSQL SystemConfig.

    Returns True if migration was performed, False if skipped.
    """
    from core.system_config import SystemConfig

    # 1. Check marker
    marker = await SystemConfig.get_param(_MARKER_CLAUDE_SETTINGS_KEY)
    if marker:
        return False

    # 2. Read file
    if not config_path.exists():
        logger.info("No claude_settings.json found — marking migration as done")
        await SystemConfig.set_param(_MARKER_CLAUDE_SETTINGS_KEY, True)
        return False

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, IOError) as exc:
        logger.error("Failed to read claude_settings.json: %s", exc)
        return False

    logger.info("Migrating claude_settings.json to PostgreSQL...")

    # 3. Store as single SystemConfig parameter
    await SystemConfig.set_param("claude_settings", config)

    # 4. Mark migration as done
    await SystemConfig.set_param(_MARKER_CLAUDE_SETTINGS_KEY, True)

    # 5. Rename file
    migrated_path = config_path.with_suffix(".json.migrated")
    try:
        config_path.rename(migrated_path)
        logger.info("Renamed claude_settings.json -> claude_settings.json.migrated")
    except OSError as exc:
        logger.warning("Could not rename claude_settings.json: %s", exc)

    logger.info("Migrated claude_settings.json to PostgreSQL successfully")
    return True


async def migrate_mcp_perms_to_unified_id() -> bool:
    """Backfill user_mcp_permissions.unified_id from old username column.

    For existing rows where unified_id is NULL but username is populated,
    resolves the unified_id via user_identities mapping (or falls back to
    using the username value as-is).

    Returns True if migration was performed, False if skipped.
    """
    from core.registry import get_database
    from core.system_config import SystemConfig

    marker = await SystemConfig.get_param(_MARKER_MCP_PERMS_UNIFIED_ID)
    if marker:
        return False

    db = get_database()
    async with db.acquire() as conn:
        # Fresh-install guard: the backfill JOINs against app.user_identities,
        # a legacy table that only exists on upgraded installs. A brand-new
        # database has the target table (user_mcp_permissions, ORM-managed)
        # AND still has the `username` column (it's a current model field,
        # not legacy), so the column check alone is not enough to bail out —
        # without this guard the subsequent SQL raises UndefinedTable and
        # kills the boot sequence on every fresh deploy.
        ui_check = await conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'app' AND table_name = 'user_identities'"
        )
        if not await ui_check.fetchone():
            logger.info(
                "app.user_identities not found — no legacy mapping to "
                "migrate; marking %s complete.",
                _MARKER_MCP_PERMS_UNIFIED_ID,
            )
            await SystemConfig.set_param(_MARKER_MCP_PERMS_UNIFIED_ID, True)
            return False

        # Check if the old username column still exists
        col_check = await conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'app' AND table_name = 'user_mcp_permissions' "
            "AND column_name = 'username'"
        )
        if not await col_check.fetchone():
            # No old column — nothing to migrate
            await SystemConfig.set_param(_MARKER_MCP_PERMS_UNIFIED_ID, True)
            return False

        # Backfill: resolve username -> unified_id via user_identities
        # Step 1: resolve all target unified_ids and deduplicate.
        # Multiple platform usernames may map to the same unified_id,
        # so we keep only the first row per (target_uid, server_name).
        await conn.execute(
            """
            WITH resolved AS (
                SELECT p.id,
                       COALESCE(
                           (SELECT ui.unified_id FROM app.user_identities ui
                            WHERE LOWER(ui.username) = LOWER(p.username) LIMIT 1),
                           p.username
                       ) AS target_uid,
                       p.server_name,
                       ROW_NUMBER() OVER (
                           PARTITION BY
                               COALESCE(
                                   (SELECT ui.unified_id FROM app.user_identities ui
                                    WHERE LOWER(ui.username) = LOWER(p.username) LIMIT 1),
                                   p.username
                               ),
                               p.server_name
                           ORDER BY p.id
                       ) AS rn
                FROM app.user_mcp_permissions p
                WHERE p.unified_id IS NULL AND p.username IS NOT NULL
            )
            DELETE FROM app.user_mcp_permissions
            WHERE id IN (SELECT r.id FROM resolved r WHERE r.rn > 1)
            """
        )

        # Step 2: also delete rows that conflict with already-migrated rows
        await conn.execute(
            """
            WITH resolved AS (
                SELECT p.id,
                       COALESCE(
                           (SELECT ui.unified_id FROM app.user_identities ui
                            WHERE LOWER(ui.username) = LOWER(p.username) LIMIT 1),
                           p.username
                       ) AS target_uid,
                       p.server_name
                FROM app.user_mcp_permissions p
                WHERE p.unified_id IS NULL AND p.username IS NOT NULL
            )
            DELETE FROM app.user_mcp_permissions
            WHERE id IN (
                SELECT r.id FROM resolved r
                WHERE EXISTS (
                    SELECT 1 FROM app.user_mcp_permissions existing
                    WHERE existing.unified_id = r.target_uid
                    AND existing.server_name = r.server_name
                    AND existing.id != r.id
                )
            )
            """
        )

        # Step 3: update remaining rows
        result = await conn.execute(
            """
            UPDATE app.user_mcp_permissions p
            SET unified_id = COALESCE(
                (SELECT ui.unified_id FROM app.user_identities ui
                 WHERE LOWER(ui.username) = LOWER(p.username)
                 LIMIT 1),
                p.username
            )
            WHERE p.unified_id IS NULL AND p.username IS NOT NULL
            """
        )
        count = result.rowcount if hasattr(result, "rowcount") else 0
        if count:
            logger.info(
                "Migrated %d MCP permission rows: username -> unified_id", count
            )

    await SystemConfig.set_param(_MARKER_MCP_PERMS_UNIFIED_ID, True)
    logger.info("MCP permissions unified_id migration complete")
    return True


async def migrate_create_default_company() -> bool:
    """Create the default company and assign all existing users to it.

    Steps:
      1. Create default company (id=1, slug="default")
      2. Insert CompanyUser for each existing app.users row

    Returns True if migration was performed, False if skipped.
    """
    from core.registry import get_database
    from core.system_config import SystemConfig

    marker = await SystemConfig.get_param(_MARKER_DEFAULT_COMPANY)
    if marker:
        return False

    db = get_database()
    async with db.acquire() as conn:
        # Check if companies table exists (ORM should have created it)
        tbl_check = await conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'app' AND table_name = 'companies'"
        )
        if not await tbl_check.fetchone():
            logger.warning("app.companies table not found — skipping company migration")
            await SystemConfig.set_param(_MARKER_DEFAULT_COMPANY, True)
            return False

        # 1. Create default company with OVERRIDING SYSTEM VALUE to force id=1
        await conn.execute(
            """
            INSERT INTO app.companies (id, name, slug, active, locale, timezone)
            OVERRIDING SYSTEM VALUE
            VALUES (1, 'Default', 'default', TRUE, 'en', 'UTC')
            ON CONFLICT (id) DO NOTHING
            """
        )

        # Reset the sequence to avoid id collision on next insert
        await conn.execute(
            "SELECT setval(pg_get_serial_sequence('app.companies', 'id'), "
            "GREATEST((SELECT MAX(id) FROM app.companies), 1))"
        )

        # 2. Insert CompanyUser for each existing user
        result = await conn.execute(
            """
            INSERT INTO app.company_users (company_id, user_id, role, is_default)
            SELECT 1, u.id,
                   CASE WHEN u.is_superadmin THEN 'owner' ELSE 'admin' END,
                   TRUE
            FROM app.users u
            WHERE NOT EXISTS (
                SELECT 1 FROM app.company_users cu
                WHERE cu.company_id = 1 AND cu.user_id = u.id
            )
            """
        )
        count = result.rowcount if hasattr(result, "rowcount") else 0
        if count:
            logger.info("Assigned %d users to default company", count)

    await SystemConfig.set_param(_MARKER_DEFAULT_COMPANY, True)
    logger.info("Default company migration complete")
    return True


async def migrate_seed_languages() -> bool:
    """Seed i18n.languages with English (default) and Italian on first boot.

    The Language ORM model auto-creates the table; this seed populates the
    minimum set so /admin/languages is functional out of the box. Idempotent
    via SystemConfig marker.

    Returns True if seed was performed, False if skipped.
    """
    from core.registry import get_database
    from core.system_config import SystemConfig

    marker = await SystemConfig.get_param(_MARKER_SEED_LANGUAGES)
    if marker:
        return False

    db = get_database()
    async with db.acquire() as conn:
        # Defensive: ORM should have created the table, skip cleanly if not
        tbl_check = await conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'i18n' AND table_name = 'languages'"
        )
        if not await tbl_check.fetchone():
            logger.warning("i18n.languages table not found — skipping language seed")
            return False

        await conn.execute(
            """
            INSERT INTO i18n.languages (code, name, active, is_default)
            VALUES ('en', 'English', TRUE, TRUE),
                   ('it', 'Italiano', TRUE, FALSE)
            ON CONFLICT (code) DO NOTHING
            """
        )

    await SystemConfig.set_param(_MARKER_SEED_LANGUAGES, True)
    logger.info("Language seed complete (en, it)")
    return True


async def migrate_rename_theme_enterprise_to_corporate() -> bool:
    """Rebind active_theme from 'theme-enterprise' to 'theme-corporate'.

    The legacy theme plugin was renamed to free the 'theme-enterprise' name
    for the dedicated Enterprise design system shipped via gridbear-enterprise.
    Existing deployments that had the legacy theme active would otherwise fall
    back to no theme after the rename. Idempotent via SystemConfig marker.

    Returns True if the rebind was performed (or skipped because not needed),
    False only when the marker was already set on a previous boot.
    """
    from core.system_config import SystemConfig

    marker = await SystemConfig.get_param(_MARKER_RENAME_THEME_ENTERPRISE)
    if marker:
        return False

    current = await SystemConfig.get_param("active_theme")
    if current == "theme-enterprise":
        await SystemConfig.set_param("active_theme", "theme-corporate")
        logger.info("active_theme rebound: theme-enterprise -> theme-corporate")

    await SystemConfig.set_param(_MARKER_RENAME_THEME_ENTERPRISE, True)
    return True


async def migrate_unify_users() -> bool:
    """Unify admin.users and app.users into a single app.users table.

    Steps:
      1. Copy admin.users rows into app.users (matching by username)
      2. Build ID mapping (admin.users.id → app.users.id) via username
      3. Re-point FKs in admin.sessions, recovery_codes, webauthn_credentials, audit_log
      4. Drop old FK constraints, add new ones pointing to app.users
      5. Assign migrated users to default company
      6. Drop admin.users table

    Returns True if migration was performed, False if skipped.
    """
    from core.registry import get_database
    from core.system_config import SystemConfig

    marker = await SystemConfig.get_param(_MARKER_UNIFY_USERS)
    if marker:
        return False

    db = get_database()
    async with db.acquire() as conn:
        # Check if admin.users table exists — if not, nothing to migrate
        tbl_check = await conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'admin' AND table_name = 'users'"
        )
        if not await tbl_check.fetchone():
            logger.info("admin.users not found — skipping user unification")
            await SystemConfig.set_param(_MARKER_UNIFY_USERS, True)
            return False

        # Check if app.users table exists (ORM should have created it)
        tbl_check2 = await conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'app' AND table_name = 'users'"
        )
        if not await tbl_check2.fetchone():
            logger.warning("app.users not found — skipping user unification")
            await SystemConfig.set_param(_MARKER_UNIFY_USERS, True)
            return False

        # 1. Ensure app.users has all required columns (belt-and-suspenders)
        for col, col_type in [
            ("username", "TEXT"),
            ("company_id", "INTEGER"),
        ]:
            col_check = await conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'users' "
                "AND column_name = %s",
                (col,),
            )
            if not await col_check.fetchone():
                await conn.execute(
                    f'ALTER TABLE app.users ADD COLUMN "{col}" {col_type}'
                )
                logger.info("Added column %s to app.users", col)

        # 1b. Drop legacy UNIQUE constraint on unified_id if present.
        # The User model defines unified_id as index=True (not unique),
        # but old migrations may have created a UNIQUE constraint.
        legacy_uq = await conn.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema = 'app' AND table_name = 'users' "
            "AND constraint_type = 'UNIQUE' AND constraint_name LIKE %s",
            ("%unified_id%",),
        )
        async for row in legacy_uq:
            cname = row["constraint_name"]
            await conn.execute(f'ALTER TABLE app.users DROP CONSTRAINT "{cname}"')
            logger.info("Dropped legacy UNIQUE constraint %s on app.users", cname)

        # 1c. Drop NOT NULL on unified_id if present (it should be nullable)
        await conn.execute(
            "ALTER TABLE app.users ALTER COLUMN unified_id DROP NOT NULL"
        )

        # 2. Merge admin.users → app.users
        # Strategy: match by unified_id first (existing bot users),
        # then by username, then INSERT if no match.

        # 2a. UPDATE existing app.users rows that match by unified_id
        # Merges auth fields (password, totp, etc.) from admin.users into
        # matching app.users rows. Uses unified_id as the login username
        # (not the old admin username) to preserve the unified identity.
        result = await conn.execute(
            """
            UPDATE app.users u SET
                username = COALESCE(u.username, u.unified_id),
                password_hash = COALESCE(au.password_hash, u.password_hash),
                email = COALESCE(au.email, u.email),
                totp_secret = COALESCE(au.totp_secret, u.totp_secret),
                totp_enabled = COALESCE(au.totp_enabled, u.totp_enabled),
                is_active = au.is_active,
                is_superadmin = au.is_superadmin,
                display_name = COALESCE(au.display_name, u.display_name),
                avatar_url = COALESCE(au.avatar_url, u.avatar_url),
                locale = COALESCE(au.locale, u.locale),
                webauthn_enabled = COALESCE(au.webauthn_enabled, u.webauthn_enabled),
                failed_login_attempts = au.failed_login_attempts,
                lockout_until = au.lockout_until,
                last_login = COALESCE(au.last_login, u.last_login)
            FROM admin.users au
            WHERE au.unified_id IS NOT NULL
              AND u.unified_id = au.unified_id
            """
        )
        merged = result.rowcount if hasattr(result, "rowcount") else 0
        if merged:
            logger.info("Merged %d admin users into app.users by unified_id", merged)

        # 2b. For admin.users WITHOUT unified_id: use admin username as both
        result = await conn.execute(
            """
            INSERT INTO app.users (
                username, email, password_hash, totp_secret, totp_enabled,
                is_active, is_superadmin, unified_id, display_name,
                avatar_url, locale, webauthn_enabled, created_at,
                last_login, failed_login_attempts, lockout_until
            )
            SELECT
                COALESCE(au.unified_id, au.username),
                au.email, au.password_hash, au.totp_secret,
                au.totp_enabled, au.is_active, au.is_superadmin,
                COALESCE(au.unified_id, au.username),
                au.display_name, au.avatar_url, au.locale, au.webauthn_enabled,
                au.created_at, au.last_login, au.failed_login_attempts,
                au.lockout_until
            FROM admin.users au
            WHERE NOT EXISTS (
                SELECT 1 FROM app.users u
                WHERE (au.unified_id IS NOT NULL AND u.unified_id = au.unified_id)
                   OR (au.username IS NOT NULL AND u.username = au.username)
            )
            """
        )
        inserted = result.rowcount if hasattr(result, "rowcount") else 0
        if inserted:
            logger.info("Inserted %d new users from admin.users", inserted)

        # 2c. Set username = unified_id for any app.users rows still missing username
        await conn.execute(
            """
            UPDATE app.users SET username = unified_id
            WHERE (username IS NULL OR username = '')
              AND unified_id IS NOT NULL
            """
        )

        # 3. Build ID mapping: admin.users.id → app.users.id
        # Match by unified_id first, fallback to username
        mapping_rows = await conn.execute(
            """
            SELECT au.id AS old_id, u.id AS new_id
            FROM admin.users au
            JOIN app.users u
              ON (au.unified_id IS NOT NULL AND u.unified_id = au.unified_id)
              OR (au.username IS NOT NULL AND u.username = au.username)
            """
        )
        id_mapping = {}
        async for row in mapping_rows:
            id_mapping[row["old_id"]] = row["new_id"]

        if id_mapping:
            logger.info(
                "User ID mapping: %s",
                ", ".join(f"{old}->{new}" for old, new in id_mapping.items()),
            )

            # 4. Re-point FKs in admin tables
            case_parts = " ".join(
                f"WHEN {old} THEN {new}" for old, new in id_mapping.items()
            )
            case_expr = f"CASE user_id {case_parts} ELSE user_id END"

            for table in [
                "admin.sessions",
                "admin.recovery_codes",
                "admin.webauthn_credentials",
            ]:
                tbl_exists = await conn.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    tuple(table.split(".")),
                )
                if await tbl_exists.fetchone():
                    result = await conn.execute(
                        f"UPDATE {table} SET user_id = {case_expr} "
                        f"WHERE user_id IN "
                        f"({','.join(str(k) for k in id_mapping)})"
                    )
                    updated = result.rowcount if hasattr(result, "rowcount") else 0
                    if updated:
                        logger.info("Remapped %d rows in %s", updated, table)

            # Also remap audit_log (user_id is nullable, no FK constraint)
            audit_exists = await conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'admin' AND table_name = 'audit_log'"
            )
            if await audit_exists.fetchone():
                result = await conn.execute(
                    f"UPDATE admin.audit_log SET user_id = {case_expr} "
                    f"WHERE user_id IN "
                    f"({','.join(str(k) for k in id_mapping)})"
                )
                updated = result.rowcount if hasattr(result, "rowcount") else 0
                if updated:
                    logger.info("Remapped %d rows in admin.audit_log", updated)
        else:
            logger.info("No admin users to remap — skipping FK updates")

        # 5. Drop old FK constraints and add new ones pointing to app.users
        fk_tables = [
            "admin.sessions",
            "admin.recovery_codes",
            "admin.webauthn_credentials",
        ]
        for table in fk_tables:
            schema, tname = table.split(".")
            # Find existing FK constraints referencing admin.users
            fk_rows = await conn.execute(
                """
                SELECT tc.constraint_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_name = ccu.constraint_name
                  AND tc.table_schema = ccu.table_schema
                WHERE tc.table_schema = %s
                  AND tc.table_name = %s
                  AND tc.constraint_type = 'FOREIGN KEY'
                  AND ccu.table_schema = 'admin'
                  AND ccu.table_name = 'users'
                """,
                (schema, tname),
            )
            async for fk_row in fk_rows:
                constraint_name = fk_row["constraint_name"]
                await conn.execute(
                    f'ALTER TABLE {table} DROP CONSTRAINT "{constraint_name}"'
                )
                logger.info("Dropped FK constraint %s on %s", constraint_name, table)

            # Add new FK pointing to app.users (skip if already exists)
            new_constraint = f"fk_{tname}_user_id_app_users"
            existing = await conn.execute(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE constraint_name = %s AND table_schema = %s",
                (new_constraint, schema),
            )
            if not await existing.fetchone():
                await conn.execute(
                    f"ALTER TABLE {table} "
                    f'ADD CONSTRAINT "{new_constraint}" '
                    f'FOREIGN KEY ("user_id") REFERENCES "app"."users"(id) '
                    f"ON DELETE CASCADE"
                )
                logger.info("Added FK %s on %s -> app.users", new_constraint, table)

        # 6. Assign migrated users to default company (company_id + CompanyUser)
        await conn.execute(
            """
            UPDATE app.users SET company_id = 1
            WHERE company_id IS NULL
            AND EXISTS (SELECT 1 FROM app.companies WHERE id = 1)
            """
        )
        await conn.execute(
            """
            INSERT INTO app.company_users (company_id, user_id, role, is_default)
            SELECT 1, u.id,
                   CASE WHEN u.is_superadmin THEN 'owner' ELSE 'admin' END,
                   TRUE
            FROM app.users u
            WHERE EXISTS (SELECT 1 FROM app.companies WHERE id = 1)
            AND NOT EXISTS (
                SELECT 1 FROM app.company_users cu
                WHERE cu.company_id = 1 AND cu.user_id = u.id
            )
            """
        )

        # 7. Drop admin.users — no longer needed
        await conn.execute("DROP TABLE IF EXISTS admin.users CASCADE")
        logger.info("Dropped admin.users table")

    await SystemConfig.set_param(_MARKER_UNIFY_USERS, True)
    logger.info("User unification migration complete (admin.users -> app.users)")
    return True


async def migrate_user_platforms() -> bool:
    """Migrate user_identities → user_platforms (FK-based) and user_profiles locale → User.locale.

    Steps:
      1. Copy identities: JOIN user_identities with users by username to get user_id
      2. Copy locales: UPDATE users.locale from user_profiles

    Returns True if migration was performed, False if skipped.
    """
    from core.registry import get_database
    from core.system_config import SystemConfig

    marker = await SystemConfig.get_param(_MARKER_USER_PLATFORMS)
    if marker:
        return False

    db = get_database()
    async with db.acquire() as conn:
        # Check if source tables exist
        for table in ("user_identities", "user_platforms"):
            tbl_check = await conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'app' AND table_name = %s",
                (table,),
            )
            if table == "user_identities" and not await tbl_check.fetchone():
                logger.info("app.user_identities not found — skipping")
                await SystemConfig.set_param(_MARKER_USER_PLATFORMS, True)
                return False
            if table == "user_platforms" and not await tbl_check.fetchone():
                logger.info(
                    "app.user_platforms not found — skipping (ORM not yet created)"
                )
                await SystemConfig.set_param(_MARKER_USER_PLATFORMS, True)
                return False

        # 1. Copy identities → user_platforms
        result = await conn.execute(
            """
            INSERT INTO app.user_platforms (user_id, platform, platform_username)
            SELECT u.id, ui.platform, ui.username
            FROM app.user_identities ui
            JOIN app.users u ON LOWER(u.username) = LOWER(ui.unified_id)
            ON CONFLICT (platform, platform_username) DO NOTHING
            """
        )
        copied = result.rowcount if hasattr(result, "rowcount") else 0
        if copied:
            logger.info("Copied %d identities to user_platforms", copied)

        # 2. Copy locales from user_profiles → users.locale
        profiles_check = await conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'app' AND table_name = 'user_profiles'"
        )
        if await profiles_check.fetchone():
            result = await conn.execute(
                """
                UPDATE app.users u SET locale = up.locale
                FROM app.user_profiles up
                WHERE LOWER(u.username) = LOWER(up.unified_id)
                AND up.locale IS NOT NULL AND up.locale != ''
                """
            )
            locales = result.rowcount if hasattr(result, "rowcount") else 0
            if locales:
                logger.info("Copied %d locales from user_profiles to users", locales)

    await SystemConfig.set_param(_MARKER_USER_PLATFORMS, True)
    logger.info("User platforms migration complete (user_identities → user_platforms)")
    return True


async def migrate_agent_configs_to_db() -> None:
    """Migrate agent YAML files to app.agent_configs DB table.

    Idempotent: checks _migrations marker, skips existing records,
    renames migrated files to .migrated.
    """
    from core.models.agent_config import AgentConfigRecord
    from core.orm.model import get_database

    db = get_database()

    # Check migration marker
    with db.acquire_sync() as conn:
        row = conn.execute(
            "SELECT 1 FROM public._migrations WHERE name = %s",
            ("_migration_agent_configs",),
        ).fetchone()
        if row:
            conn.rollback()
            logger.info("Agent config migration already applied, skipping")
            return
        conn.rollback()

    agents_dir = Path(__file__).resolve().parent.parent / "config" / "agents"
    if not agents_dir.exists():
        logger.info("No agents directory found, skipping agent config migration")
        return

    yaml_files = sorted(agents_dir.glob("*.yaml"))
    if not yaml_files:
        logger.info("No agent YAML files found, skipping migration")
        return

    migrated_count = 0
    for yaml_file in yaml_files:
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)

            if not data or not isinstance(data, dict):
                logger.warning("Skipping invalid YAML: %s", yaml_file.name)
                continue

            agent_id = data.get("id", yaml_file.stem)

            # Skip if already in DB
            existing = AgentConfigRecord.get_sync(id=agent_id)
            if existing:
                logger.info("Agent %s already in DB, skipping", agent_id)
                continue

            # Normalize plugins field
            plugins_cfg = data.get("plugins", {})
            if isinstance(plugins_cfg, list):
                plugins_cfg = {"enabled": plugins_cfg}
            elif not isinstance(plugins_cfg, dict):
                plugins_cfg = {}

            AgentConfigRecord.create_sync(
                id=agent_id,
                name=data.get("name", agent_id),
                description=data.get("description", ""),
                personality=data.get("personality", ""),
                locale=data.get("locale", "en"),
                timezone=data.get("timezone", "UTC"),
                runner=data.get("runner", "claude"),
                model=data.get("model", ""),
                fallback_runner=data.get("fallback_runner", ""),
                tool_loading=data.get("tool_loading", "full"),
                max_tools=int(data.get("max_tools", 0)),
                avatar=data.get("avatar", ""),
                is_active=True,
                channels=data.get("channels") or {},
                services=data.get("services") or [],
                voice=data.get("voice") or {},
                image=data.get("image") or {},
                email=data.get("email") or {},
                mcp_permissions=data.get("mcp_permissions") or [],
                plugins=plugins_cfg,
                context_options=data.get("context_options") or {},
            )

            # Rename to .migrated
            migrated_path = yaml_file.with_suffix(".migrated")
            yaml_file.rename(migrated_path)
            migrated_count += 1
            logger.info("Migrated agent %s to DB", agent_id)

        except Exception as e:
            logger.error("Failed to migrate %s: %s", yaml_file.name, e)

    # Record migration marker
    if migrated_count > 0:
        with db.acquire_sync() as conn:
            conn.execute(
                "INSERT INTO public._migrations (name) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                ("_migration_agent_configs",),
            )
            conn.commit()

    logger.info("Agent config migration: %d agents migrated to DB", migrated_count)
