"""
Database schema migration management.

Applies pending migrations on startup. Migrations are forward-only
and non-destructive. The application refuses to start if a migration
cannot be safely applied.
"""

import logging
from typing import Callable

import asyncpg

from app.database import get_pg_pool

_log = logging.getLogger("pmad_template.migrations")


async def _migration_001(conn) -> None:
    """Migration 1: Initial schema.

    The initial schema is applied by postgres/init.sql via the Docker
    entrypoint. This migration just records that it was applied.
    """
    pass


async def _migration_002(conn) -> None:
    """Migration 2: Add stategraph_packages table for dynamic loading (REQ-001 §10).

    Tracks which StateGraph packages are installed and their versions.
    Used by install_stategraph() to record installations.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS stategraph_packages (
            package_name  VARCHAR(255) PRIMARY KEY,
            version       VARCHAR(100) NOT NULL,
            installed_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            entry_point_group VARCHAR(100),
            metadata      JSONB
        )
    """)
    _log.info("Migration 002 complete — stategraph_packages table")


async def _migration_003(conn) -> None:
    """Migration 3: Create alert_instructions table for alerter sidecar tools.

    The Imperator's alerting tools (add/list/update/delete_alert_instruction)
    store instructions in this table. The alerter sidecar reads them to
    decide how to format and route alerts.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_instructions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            description     TEXT NOT NULL,
            instruction     TEXT NOT NULL,
            channels        JSONB NOT NULL DEFAULT '[]'::jsonb,
            embedding       vector,
            created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """)
    _log.info("Migration 003 complete — alert_instructions table")


async def _migration_004(conn) -> None:
    """Migration 4: Create domain_information table with auto-embedding trigger.

    Stores domain facts with vector embeddings for semantic search.
    Postgres trigger fires NOTIFY on insert — the AE's LISTEN callback
    invokes the embedding stategraph to generate the vector automatically.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS domain_information (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            content     TEXT NOT NULL,
            source      VARCHAR(100) NOT NULL DEFAULT 'host',
            embedding   vector(768),
            created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_domain_info_source
            ON domain_information(source)
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_domain_info_embedding
            ON domain_information USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)
    """)
    # Trigger: notify AE on new domain info for auto-embedding
    await conn.execute("""
        CREATE OR REPLACE FUNCTION notify_domain_info_new()
        RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('domain_info_new', NEW.id::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    await conn.execute("""
        DROP TRIGGER IF EXISTS trg_domain_info_new ON domain_information
    """)
    await conn.execute("""
        CREATE TRIGGER trg_domain_info_new
            AFTER INSERT ON domain_information
            FOR EACH ROW
            EXECUTE FUNCTION notify_domain_info_new()
    """)
    _log.info("Migration 004 complete — domain_information table with auto-embedding trigger")


async def _migration_005(conn) -> None:
    """Migration 5: Create emad_instances table for eMAD routing.

    Maps model names to their TE package and active status.
    Used by the chat route to look up which package handles a given model name.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS emad_instances (
            emad_name     VARCHAR(255) PRIMARY KEY,
            package_name  VARCHAR(255) NOT NULL,
            status        VARCHAR(50)  NOT NULL DEFAULT 'active',
            installed_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            metadata      JSONB
        )
    """)
    _log.info("Migration 005 complete — emad_instances table")


# Migration registry: version -> (description, migration_function)
# Add new migrations here. Never modify existing entries.
# IMPORTANT: This list MUST appear after all _migration_NNN function definitions.
MIGRATIONS: list[tuple[int, str, Callable]] = [
    (1, "Initial schema — created by postgres/init.sql", _migration_001),
    (
        2,
        "Add stategraph_packages table for dynamic loading (REQ-001 §10)",
        _migration_002,
    ),
    (
        3,
        "Create alert_instructions table for alerter sidecar tools",
        _migration_003,
    ),
    (
        4,
        "Create domain_information table with auto-embedding trigger",
        _migration_004,
    ),
    (
        5,
        "Create emad_instances table for eMAD routing",
        _migration_005,
    ),
]


async def get_current_schema_version(conn) -> int:
    """Return the highest applied migration version, or 0 if none."""
    try:
        version = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        )
        return version or 0
    except asyncpg.UndefinedTableError:
        # schema_migrations table doesn't exist yet — fresh database
        return 0


async def run_migrations() -> None:
    """Apply all pending migrations in order.

    R5-m12: Uses a PostgreSQL advisory lock to serialize migrations when
    multiple workers start simultaneously. Advisory lock ID 1 is reserved
    for schema migrations.

    Raises RuntimeError if any migration fails, preventing startup
    with an incompatible schema.
    """
    pool = get_pg_pool()

    async with pool.acquire() as conn:
        # R5-m12: Acquire advisory lock to prevent concurrent migration runs
        await conn.execute("SELECT pg_advisory_lock(1)")
        try:
            current_version = await get_current_schema_version(conn)
            _log.info("Current schema version: %d", current_version)

            pending = [
                (version, description, fn)
                for version, description, fn in MIGRATIONS
                if version > current_version
            ]

            if not pending:
                _log.info("Schema is up to date (version %d)", current_version)
                return

            for version, description, migration_fn in pending:
                _log.info("Applying migration %d: %s", version, description)
                try:
                    async with conn.transaction():
                        await migration_fn(conn)
                        await conn.execute(
                            """
                            INSERT INTO schema_migrations (version, description)
                            VALUES ($1, $2)
                            ON CONFLICT (version) DO NOTHING
                            """,
                            version,
                            description,
                        )
                    _log.info("Migration %d applied successfully", version)
                except (asyncpg.PostgresError, OSError) as exc:
                    raise RuntimeError(
                        f"Migration {version} ('{description}') failed: {exc}. "
                        "Cannot start with incompatible schema."
                    ) from exc

            _log.info(
                "Schema migrations complete. Now at version %d",
                pending[-1][0],
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock(1)")
