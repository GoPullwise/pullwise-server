from __future__ import annotations

import sqlite3


def install_model_gateway_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_connections (
            provider_connection_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            provider TEXT NOT NULL,
            adapter TEXT NOT NULL,
            endpoint_origin TEXT NOT NULL,
            secret_ref TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            secret_version TEXT NOT NULL,
            secret_fingerprint TEXT NOT NULL,
            validated_models_json TEXT NOT NULL,
            candidate_secret_version TEXT,
            candidate_secret_fingerprint TEXT,
            previous_secret_version TEXT,
            previous_secret_fingerprint TEXT,
            last_validated_at INTEGER,
            last_rotated_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_secret_versions (
            provider_connection_id TEXT NOT NULL,
            secret_version TEXT NOT NULL,
            secret_fingerprint TEXT NOT NULL,
            validated_models_json TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            validated_at INTEGER,
            promoted_at INTEGER,
            retired_at INTEGER,
            PRIMARY KEY (provider_connection_id, secret_version),
            FOREIGN KEY (provider_connection_id)
                REFERENCES provider_connections(provider_connection_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS model_gateway_audit_events (
            id TEXT PRIMARY KEY,
            actor_user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            changed_fields_json TEXT NOT NULL,
            request_id TEXT,
            created_at INTEGER NOT NULL,
            success INTEGER NOT NULL,
            error_code TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS model_gateway_secret_cleanup_tasks (
            secret_ref TEXT NOT NULL,
            secret_version TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            last_attempt_at INTEGER NOT NULL,
            retired_at INTEGER,
            PRIMARY KEY (secret_ref, secret_version)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_model_gateway_audit_subject
        ON model_gateway_audit_events(subject_type, subject_id, created_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_token_grants (
            jti TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            profile_set_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL,
            manifest_digest TEXT NOT NULL,
            route_ids_json TEXT NOT NULL,
            generation INTEGER NOT NULL,
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gateway_token_grants_worker
        ON gateway_token_grants(worker_id, generation, expires_at, revoked_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_sets (
            profile_set_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL,
            active_revision INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_set_revisions (
            profile_set_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            manifest_digest TEXT NOT NULL,
            created_by_user_id TEXT NOT NULL,
            request_id TEXT,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (profile_set_id, revision),
            UNIQUE (profile_set_id, manifest_digest),
            FOREIGN KEY (profile_set_id) REFERENCES profile_sets(profile_set_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_set_routes (
            profile_set_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            route_position INTEGER NOT NULL,
            route_id TEXT NOT NULL,
            provider_connection_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_alias TEXT NOT NULL,
            upstream_model TEXT NOT NULL,
            api TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            PRIMARY KEY (profile_set_id, revision, route_id),
            UNIQUE (profile_set_id, revision, route_position),
            FOREIGN KEY (profile_set_id, revision)
                REFERENCES profile_set_revisions(profile_set_id, revision),
            FOREIGN KEY (provider_connection_id)
                REFERENCES provider_connections(provider_connection_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_pools (
            worker_pool_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            profile_set_id TEXT NOT NULL,
            desired_revision INTEGER NOT NULL,
            gateway_token_generation INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (profile_set_id, desired_revision)
                REFERENCES profile_set_revisions(profile_set_id, revision)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_pool_memberships (
            worker_id TEXT PRIMARY KEY,
            worker_pool_id TEXT NOT NULL,
            desired_revision INTEGER NOT NULL,
            bound_by_user_id TEXT NOT NULL,
            request_id TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (worker_id) REFERENCES workers(worker_id),
            FOREIGN KEY (worker_pool_id) REFERENCES worker_pools(worker_pool_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_pool_memberships_pool
        ON worker_pool_memberships(worker_pool_id, worker_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_profile_observations (
            worker_id TEXT PRIMARY KEY,
            worker_pool_id TEXT NOT NULL,
            profile_set_id TEXT NOT NULL,
            desired_revision INTEGER NOT NULL,
            applied_revision INTEGER NOT NULL,
            manifest_digest TEXT NOT NULL,
            catalog_digest TEXT NOT NULL,
            gateway_token_expires_at INTEGER NOT NULL,
            gateway_token_id TEXT NOT NULL,
            last_apply_result TEXT NOT NULL,
            applied_at INTEGER NOT NULL,
            observed_at INTEGER NOT NULL,
            FOREIGN KEY (worker_id) REFERENCES workers(worker_id),
            FOREIGN KEY (worker_pool_id) REFERENCES worker_pools(worker_pool_id),
            FOREIGN KEY (profile_set_id, desired_revision)
                REFERENCES profile_set_revisions(profile_set_id, revision)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_bootstrap_credentials (
            bootstrap_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at INTEGER NOT NULL,
            used_at INTEGER,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (worker_id) REFERENCES workers(worker_id) ON DELETE CASCADE
        )
        """
    )
