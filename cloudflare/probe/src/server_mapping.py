"""Candidate finite D1 commands over Server tables, exercised only by the probe.

No API, credentials, network, payment interpretation or production wiring.
Each returned list must be one D1 batch; never execute it as separate awaits.
"""
from datetime import datetime, timezone


def schema():
    return [("CREATE TABLE IF NOT EXISTS d1_command_guard(ok INTEGER NOT NULL CHECK(ok=1))", ())]


def _check(predicate, params=()):
    return ("INSERT INTO d1_command_guard VALUES(CASE WHEN " + predicate + " THEN 1 ELSE 0 END)", tuple(params))


def _changed():
    return _check("changes()=1")


def _account(job_id, frozen):
    # Match the actual persisted users entry, including billing facts, not an
    # in-memory plan or checkout-return URL. Equality is deliberately conservative.
    return _check("""EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u,background_jobs j
        WHERE a.name='users' AND u.key=j.billing_owner_id AND u.value=? AND j.id=?)""", (frozen, job_id))


def _job_binding(job_id, now):
    return _check("""EXISTS(SELECT 1 FROM background_jobs j
        JOIN source_records s ON s.source_id=j.source_id
        JOIN source_contexts c ON c.source_id=j.source_id AND c.context_id=j.context_id
        JOIN processing_usage_ledger l ON l.reservation_id=j.reservation_id
        WHERE j.id=? AND j.job_type='analyze_source' AND s.processing_mode='model' AND s.lifecycle='active'
        AND s.latest_version=j.source_version_id AND s.source_revision=j.source_revision
        AND c.accessible=1 AND c.analysis_enabled=1 AND c.context_stale=0
        AND c.authorization_valid_until>=? AND c.authorization_revision=j.authorization_revision
        AND c.configuration_revision=j.configuration_revision AND c.context_version=j.context_version
        AND c.billing_owner_id=j.billing_owner_id AND l.billing_owner_id=j.billing_owner_id
        AND l.state='reserved')""", (job_id, now))


def claim(*, job_id, token, now, account_snapshot, owner_monthly_limit,
          global_monthly_limit, owner_rolling_limit, global_rolling_limit):
    period = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    return [
        _account(job_id, account_snapshot), _job_binding(job_id, now),
        ("""UPDATE background_jobs SET state='running',attempt=attempt+1,claim_token=?,
            claimed_until=?,next_attempt_at=NULL,updated_at=? WHERE id=? AND attempt<3 AND (
              (state IN ('queued','retry_wait') AND COALESCE(next_attempt_at,0)<=?)
              OR (state='running' AND COALESCE(claimed_until,0)<=?))""",
         (token, now + 120, now, job_id, now, now)), _changed(),
        ("""INSERT INTO provider_attempts(attempt_id,billing_owner_id,input_key,period_utc,occurred_at,created_at)
            SELECT ?,j.billing_owner_id,j.logical_key,?,?,? FROM background_jobs j WHERE j.id=?
              AND (SELECT COUNT(*) FROM provider_attempts WHERE period_utc=?)<?
              AND (SELECT COUNT(*) FROM provider_attempts WHERE period_utc=? AND billing_owner_id=j.billing_owner_id)<?
              AND (SELECT COUNT(*) FROM provider_attempts WHERE occurred_at BETWEEN ? AND ?)<?
              AND (SELECT COUNT(*) FROM provider_attempts WHERE occurred_at BETWEEN ? AND ? AND billing_owner_id=j.billing_owner_id)<?""",
         (token, period, now, now, job_id, period, global_monthly_limit, period, owner_monthly_limit,
          max(0, now - 59), now, global_rolling_limit, max(0, now - 59), now, owner_rolling_limit)), _changed(),
        ("""UPDATE source_contexts SET processing_status='processing',updated_at=? WHERE (source_id,context_id)=
            (SELECT source_id,context_id FROM background_jobs WHERE id=?)""", (now, job_id)), _changed(),
        ("""INSERT INTO analysis_claim_owners(billing_owner_id,last_claim_order)
            SELECT billing_owner_id,(SELECT COALESCE(MAX(last_claim_order),0)+1 FROM analysis_claim_owners)
            FROM background_jobs WHERE id=? ON CONFLICT(billing_owner_id) DO UPDATE SET last_claim_order=excluded.last_claim_order""", (job_id,)),
        ("DELETE FROM d1_command_guard", ()),
    ]


def publication(*, job_id, token, now, account_snapshot, sources, fences,
                assessment, source_publication, item, expected_item_revision, version):
    """Publish a prevalidated immutable result; CAS every read used to build it.

    Rows use actual SQLite column names. Assessment validation/semantic projection
    remains in Server; this experiment validates the persistence boundary only.
    """
    commands = [_account(job_id, account_snapshot), _job_binding(job_id, now),
        _check("""EXISTS(SELECT 1 FROM background_jobs WHERE id=? AND state='running'
            AND claim_token=? AND claimed_until>?)""", (job_id, token, now))]
    if len({s["sourceId"] for s in sources}) != len(sources) or {s["sourceId"] for s in sources} != {f["sourceId"] for f in fences}:
        raise ValueError("incomplete dependency fences")
    for source in sources:
        commands.append(_check("""EXISTS(SELECT 1 FROM source_records WHERE source_id=?
            AND latest_version=? AND source_revision=?)""",
            (source["sourceId"], source["sourceVersion"], source["sourceRevision"])))
    for fence in fences:
        commands.append(_check("""EXISTS(SELECT 1 FROM source_contexts c,background_jobs j WHERE j.id=?
            AND c.source_id=? AND c.context_id=? AND c.context_version=? AND c.configuration_revision=?
            AND c.authorization_revision=? AND c.authorization_valid_until>=? AND c.accessible=1
            AND c.analysis_enabled=1 AND c.context_stale=0 AND c.billing_owner_id=j.billing_owner_id)""",
            (job_id, fence["sourceId"], fence["contextId"], fence["contextVersion"],
             fence["configurationRevision"], fence["authorizationRevision"], now)))
    commands.append(_check("""EXISTS(SELECT 1 FROM background_jobs WHERE id=? AND source_id=? AND
        source_version_id=? AND context_id=? AND billing_owner_id=?)""",
        (job_id, source_publication["source_id"], assessment["source_version_id"],
         source_publication["context_id"], assessment["billing_owner_id"])))
    # Finite column allowlists prevent this module becoming an arbitrary SQL API.
    for table, row, columns in (
        ("assessments", assessment, "id billing_owner_id source_version_id context_hash evaluated_context_version question_version extractor_version model input_hash dependencies_json answers_json usage_json status created_at"),
        ("source_assessment_publications", source_publication, "source_id context_id source_version_id context_version authorization_revision billing_owner_id assessment_json evidence_json sources_json fences_json coverage_json"),
        ("item_versions", version, "item_id item_version snapshot_hash sources_json context_fences_json snapshot_json observed_at"),
    ):
        names = columns.split()
        commands.append((f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)})",
                         tuple(row[name] for name in names)))
    commands += [
        ("""UPDATE items SET current_item_version=?,current_snapshot_hash=?,revision=revision+1,updated_at=?
            WHERE id=? AND revision=? AND current_item_version=?""",
         (version["item_version"], version["snapshot_hash"], now, item["id"], expected_item_revision, version["item_version"] - 1)), _changed(),
        ("""UPDATE processing_usage_buckets SET reserved=reserved-1,used=used+1,updated_at=?
            WHERE (billing_owner_id,period)=(SELECT l.billing_owner_id,l.period FROM processing_usage_ledger l
              JOIN background_jobs j ON j.reservation_id=l.reservation_id WHERE j.id=?)
              AND metric='intelligent_processing' AND reserved>=1""", (now, job_id)), _changed(),
        ("""UPDATE processing_usage_ledger SET state='consumed',finished_at=? WHERE reservation_id=
            (SELECT reservation_id FROM background_jobs WHERE id=?) AND state='reserved'""", (now, job_id)), _changed(),
        ("UPDATE background_jobs SET state='succeeded',claim_token=NULL,claimed_until=NULL,updated_at=? WHERE id=? AND claim_token=?",
         (now, job_id, token)), _changed(),
        ("UPDATE source_contexts SET processing_status='assessed',item_id=?,updated_at=? WHERE source_id=? AND context_id=?",
         (item["id"], now, source_publication["source_id"], source_publication["context_id"])), _changed(),
        ("DELETE FROM d1_command_guard", ()),
    ]
    return commands
