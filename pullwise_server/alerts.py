from __future__ import annotations

import logging
import smtplib
import threading
import time
from email.message import EmailMessage

from . import db, github_auth, system_config

logger = logging.getLogger(__name__)

STATE_KEY = 'alert_notifications'
_SYSTEM_PROBLEM_STATUSES = {'degraded', 'down'}
_WORKER_PROBLEM_STATUSES = {'degraded', 'offline'}
_GROUPED_WORKER_PREFIX = 'workers:'
_MAX_WORKERS_IN_EMAIL = 50
_LOCK = threading.Lock()


def _clean_header(value):
    return str(value or '').replace('\r', ' ').replace('\n', ' ').strip()[:300]


def _clean_text(value, limit=500):
    return str(value or '').replace('\x00', '').strip()[:limit]


def _recipients():
    recipients = []
    seen = set()
    for item in system_config.alert_email_recipients():
        email = github_auth.clean_account_email_address(item)
        key = email.lower() if email else ''
        if not key or key in seen:
            continue
        seen.add(key)
        recipients.append(email)
    return recipients


def _sender(recipients):
    sender = (
        system_config.alert_email_from()
        or system_config.alert_smtp_username()
        or (recipients[0] if recipients else '')
        or 'pullwise@example.invalid'
    )
    return github_auth.clean_account_email_address(sender) or 'pullwise@example.invalid'


def send_alert_email(subject, body):
    if not system_config.alert_email_enabled():
        return False
    recipients = _recipients()
    host = system_config.alert_smtp_host()
    if not recipients or not host:
        logger.warning('alert email is enabled but recipient or SMTP host is missing')
        return False
    username = system_config.alert_smtp_username()
    password = system_config.alert_smtp_password()
    use_ssl = system_config.alert_smtp_ssl()
    starttls = system_config.alert_smtp_starttls()
    port = system_config.alert_smtp_port()
    if username and not password:
        logger.warning('alert email is enabled but SMTP password is missing')
        return False

    message = EmailMessage()
    message['Subject'] = _clean_header(subject)
    message['From'] = _sender(recipients)
    message['To'] = ', '.join(recipients)
    message.set_content(str(body or '').strip() or 'Pullwise alert has no details.')

    try:
        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_class(host, port, timeout=15) as smtp:
            if starttls and not use_ssl:
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    except Exception as exc:
        logger.exception('alert email delivery failed: %s', exc)
        return False
    else:
        logger.info('alert email sent to %s', ', '.join(recipients))
        return True


def _load_active():
    raw = db.load_state_item(STATE_KEY)
    if not isinstance(raw, dict):
        return {}
    active = raw.get('active')
    if not isinstance(active, dict):
        return {}
    return {str(key): value for key, value in active.items() if str(key or '').strip()}


def _save_active(active):
    db.save_state_item(STATE_KEY, {'active': active})


def _system_alert(payload):
    status = _clean_text(payload.get('scanSystemStatus')).lower()
    if status not in _SYSTEM_PROBLEM_STATUSES:
        return {}
    online = payload.get('onlineWorkerCount', 0)
    total = payload.get('totalWorkerCount', 0)
    degraded = payload.get('degradedWorkerCount', 0)
    offline = payload.get('offlineWorkerCount', 0)
    queued = payload.get('queuedJobs', 0)
    running = payload.get('runningJobs', 0)
    manual_uninstall = payload.get('administratorWorkerUninstallCount', 0)
    subject = f'Pullwise scan system {status}'
    if manual_uninstall:
        subject = f'{subject} (admin worker uninstall)'
    lines = [
        'Pullwise scan system problem detected.',
        f'Status: {status}',
        f'Online workers: {online} / {total}',
        f'Degraded workers: {degraded}',
        f'Offline workers: {offline}',
        f'Queued jobs: {queued}',
        f'Running jobs: {running}',
    ]
    if manual_uninstall:
        noun = 'command is' if manual_uninstall == 1 else 'commands are'
        lines.append(f'Administrator action: {manual_uninstall} worker uninstall {noun} pending or running.')
    return {
        f'server:scan-system:{status}': {
            'subject': subject,
            'body': '\n'.join(lines),
            'kind': 'server',
            'status': status,
        }
    }



def _quota_float(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _format_percent(value):
    number = _quota_float(value)
    if number is None:
        return 'unknown'
    return f'{number:g}%'


def _format_unix_time(value):
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return 'unknown'
    if timestamp <= 0:
        return 'unknown'
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(timestamp))


def _worker_quota_payload(worker):
    quota = worker.get('codexQuota') if isinstance(worker.get('codexQuota'), dict) else None
    if quota is None and isinstance(worker.get('codex_quota'), dict):
        quota = worker.get('codex_quota')
    return quota if isinstance(quota, dict) else {}


def _quota_problem(quota):
    if not isinstance(quota, dict):
        return False, '', []
    status = _clean_text(quota.get('status'), 40).lower()
    threshold = _quota_float(quota.get('thresholdPercent'))
    if threshold is None:
        threshold = 5.0
    windows = [item for item in quota.get('windows') or [] if isinstance(item, dict)]
    blocked = []
    for window in windows:
        remaining = _quota_float(window.get('remainingPercent'))
        if remaining is not None and remaining <= threshold:
            blocked.append(window)
    if status in {'low', 'exhausted'} or blocked:
        return True, status if status in {'low', 'exhausted'} else 'low', blocked
    return False, status, blocked


def _worker_quota_alert(worker):
    quota = _worker_quota_payload(worker)
    problem, status, blocked = _quota_problem(quota)
    if not problem:
        return {}
    worker_id = _clean_text(worker.get('worker_id') or worker.get('id'), 128)
    if not worker_id:
        return {}
    name = _clean_text(worker.get('name') or worker_id, 120)
    threshold = _format_percent(quota.get('thresholdPercent') if quota.get('thresholdPercent') is not None else 5)
    reset_credits = quota.get('rateLimitResetCredits') if isinstance(quota.get('rateLimitResetCredits'), dict) else {}
    available_resets = reset_credits.get('availableCount') if reset_credits else None
    subject = f'Pullwise workers Codex quota {status}'
    lines = [
        f'Worker: {name} ({worker_id})',
        f'Plan: {_clean_text(quota.get("planType"), 80) or "unknown"}',
        f'Threshold: {threshold} remaining',
        f'Rate limit reached type: {_clean_text(quota.get("rateLimitReachedType"), 120) or "none"}',
        f'Reset credits: {available_resets if available_resets is not None else "unknown"}',
        f'Checked at: {_format_unix_time(quota.get("checkedAt"))}',
    ]
    windows = [item for item in quota.get('windows') or [] if isinstance(item, dict)]
    if windows:
        lines.append('Quota windows:')
        for window in windows:
            label = _clean_text(window.get('label') or window.get('windowKind') or window.get('name'), 80) or 'window'
            lines.append(
                f'- {label}: used {_format_percent(window.get("usedPercent"))}, '
                f'remaining {_format_percent(window.get("remainingPercent"))}, '
                f'resets {_format_unix_time(window.get("resetsAt"))}'
            )
    if blocked:
        blocked_labels = [
            _clean_text(window.get('label') or window.get('windowKind') or window.get('name'), 80) or 'window'
            for window in blocked
        ]
        lines.append(f'Blocked windows: {", ".join(blocked_labels)}')
    last_error = _clean_text(quota.get('lastError') or worker.get('last_error'))
    if last_error:
        lines.append(f'Last error: {last_error}')
    return {
        f'workers:codex-quota:{status}': {
            'subject': subject,
            'intro': 'Pullwise worker Codex quota warning detected.',
            'members': {worker_id: '\n'.join(lines)},
            'kind': 'worker_codex_quota',
            'status': status,
        }
    }

def _worker_alert(worker):
    worker_id = _clean_text(worker.get('worker_id') or worker.get('id'), 128)
    if not worker_id:
        return {}
    status = _clean_text(worker.get('status')).lower()
    if status not in _WORKER_PROBLEM_STATUSES:
        return {}
    quota_problem, _quota_status, _blocked_windows = _quota_problem(_worker_quota_payload(worker))
    if status == 'degraded' and quota_problem:
        return {}
    name = _clean_text(worker.get('name') or worker_id, 120)
    provider = _clean_text(worker.get('provider'))
    doctor_status = _clean_text(worker.get('doctor_status'))
    codex_ready = worker.get('codex_ready')
    last_heartbeat = worker.get('last_heartbeat_at')
    subject = f'Pullwise workers {status}'
    lines = [
        f'Worker: {name} ({worker_id})',
        f'Provider: {provider}',
        f'Doctor status: {doctor_status}',
        f'Codex ready: {codex_ready}',
        f'Last heartbeat: {last_heartbeat}',
    ]
    last_error = _clean_text(worker.get('last_error'))
    if last_error:
        lines.append(f'Last error: {last_error}')
    return {
        f'workers:status:{status}': {
            'subject': subject,
            'intro': 'Pullwise worker problem detected.',
            'members': {worker_id: '\n'.join(lines)},
            'kind': 'worker',
            'status': status,
        }
    }



def _worker_alerts(worker):
    alerts = _worker_alert(worker)
    alerts.update(_worker_quota_alert(worker))
    return alerts


def _merge_alerts(target, additions):
    for key, alert in additions.items():
        existing = target.get(key)
        existing_members = existing.get('members') if isinstance(existing, dict) else None
        added_members = alert.get('members') if isinstance(alert, dict) else None
        if isinstance(existing_members, dict) and isinstance(added_members, dict):
            merged = dict(existing)
            merged['members'] = {**existing_members, **added_members}
            target[key] = merged
        else:
            target[key] = alert


def _render_alert(alert):
    subject = _clean_text(alert.get('subject'), 300)
    members = alert.get('members') if isinstance(alert.get('members'), dict) else None
    if members is None:
        return subject, str(alert.get('body') or '')
    worker_ids = sorted(str(worker_id) for worker_id in members if str(worker_id or '').strip())
    lines = [
        _clean_text(alert.get('intro'), 500) or 'Pullwise worker problem detected.',
        f'Status: {_clean_text(alert.get("status"), 60) or "unknown"}',
        f'Affected workers currently included: {len(worker_ids)}',
        'Further same-status alerts from additional workers are grouped into this incident and do not send individual emails.',
    ]
    for worker_id in worker_ids[:_MAX_WORKERS_IN_EMAIL]:
        detail = _clean_text(members.get(worker_id), 4000)
        if detail:
            lines.extend(['', detail])
    omitted = len(worker_ids) - _MAX_WORKERS_IN_EMAIL
    if omitted > 0:
        lines.extend(['', f'{omitted} additional affected workers omitted from this email.'])
    return subject, '\n'.join(lines)


def _entry_worker_ids(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get('workerIds'), list):
        return set()
    return {
        worker_id
        for item in entry.get('workerIds') or []
        if (worker_id := _clean_text(item, 128))
    }


def _entry_with_worker_ids(entry, worker_ids, alert=None):
    updated = dict(entry) if isinstance(entry, dict) else {}
    normalized_ids = sorted({_clean_text(item, 128) for item in worker_ids if _clean_text(item, 128)})
    updated['workerIds'] = normalized_ids
    updated['affectedWorkerCount'] = len(normalized_ids)
    if isinstance(alert, dict):
        updated['kind'] = _clean_text(alert.get('kind'), 60)
        updated['status'] = _clean_text(alert.get('status'), 60)
    return updated


def _legacy_worker_group(key):
    if not key.startswith('worker:'):
        return '', ''
    for status in ('low', 'exhausted'):
        suffix = f':codex-quota:{status}'
        if key.endswith(suffix):
            worker_id = _clean_text(key[len('worker:') : -len(suffix)], 128)
            return (f'workers:codex-quota:{status}', worker_id) if worker_id else ('', '')
    for status in sorted(_WORKER_PROBLEM_STATUSES):
        suffix = f':{status}'
        if key.endswith(suffix):
            worker_id = _clean_text(key[len('worker:') : -len(suffix)], 128)
            return (f'workers:status:{status}', worker_id) if worker_id else ('', '')
    return '', ''


def _normalize_active(active):
    normalized = {}
    legacy_entries = []
    for key, entry in active.items():
        group_key, worker_id = _legacy_worker_group(key)
        if group_key:
            legacy_entries.append((group_key, worker_id, entry))
            continue
        normalized[key] = entry
    for group_key, worker_id, entry in legacy_entries:
        existing = normalized.get(group_key)
        worker_ids = _entry_worker_ids(existing)
        worker_ids.add(worker_id)
        base = existing if isinstance(existing, dict) else entry
        normalized[group_key] = _entry_with_worker_ids(base, worker_ids)
    return normalized


def _sync_alerts(alerts, clear_prefixes, *, worker_ids=None, complete_worker_snapshot=False):
    with _LOCK:
        loaded_active = _load_active()
        active = _normalize_active(loaded_active)
        changed = active != loaded_active
        for key in list(active):
            if any(key.startswith(prefix) for prefix in clear_prefixes) and key not in alerts:
                active.pop(key, None)
                changed = True

        grouped_alerts = {
            key: alert
            for key, alert in alerts.items()
            if key.startswith(_GROUPED_WORKER_PREFIX) and isinstance(alert.get('members'), dict)
        }
        incoming_worker_ids = {
            key: {_clean_text(worker_id, 128) for worker_id in alert['members'] if _clean_text(worker_id, 128)}
            for key, alert in grouped_alerts.items()
        }
        if complete_worker_snapshot:
            for key in list(active):
                if key.startswith(_GROUPED_WORKER_PREFIX) and key not in grouped_alerts:
                    active.pop(key, None)
                    changed = True
            for key, next_worker_ids in incoming_worker_ids.items():
                if key not in active:
                    continue
                updated = _entry_with_worker_ids(active[key], next_worker_ids, grouped_alerts[key])
                if updated != active[key]:
                    active[key] = updated
                    changed = True
        elif worker_ids:
            updated_worker_ids = {_clean_text(item, 128) for item in worker_ids if _clean_text(item, 128)}
            group_keys = {key for key in active if key.startswith(_GROUPED_WORKER_PREFIX)} | set(grouped_alerts)
            for key in group_keys:
                current_worker_ids = _entry_worker_ids(active.get(key))
                next_worker_ids = (current_worker_ids - updated_worker_ids) | incoming_worker_ids.get(key, set())
                if key not in active:
                    continue
                if not next_worker_ids:
                    active.pop(key, None)
                    changed = True
                    continue
                updated = _entry_with_worker_ids(active[key], next_worker_ids, grouped_alerts.get(key))
                if updated != active[key]:
                    active[key] = updated
                    changed = True

        current_time = int(time.time())
        for key, alert in alerts.items():
            if key in active:
                continue
            subject, body = _render_alert(alert)
            email_delivered = send_alert_email(subject, body)
            entry = {
                'attemptedAt': current_time,
                'subject': subject,
                'kind': _clean_text(alert.get('kind'), 60),
                'status': _clean_text(alert.get('status'), 60),
                'emailDelivered': bool(email_delivered),
            }
            if key in grouped_alerts:
                entry = _entry_with_worker_ids(entry, incoming_worker_ids.get(key, set()), alert)
            if email_delivered:
                entry['sentAt'] = current_time
            active[key] = entry
            changed = True
        if changed:
            _save_active(active)


def sync_scan_system_alerts(payload, workers):
    try:
        alerts = _system_alert(payload if isinstance(payload, dict) else {})
        for worker in workers or []:
            _merge_alerts(alerts, _worker_alerts(worker if isinstance(worker, dict) else {}))
        _sync_alerts(alerts, ['server:scan-system:', 'worker:'], complete_worker_snapshot=True)
    except Exception as exc:
        logger.exception('scan system alert sync failed: %s', exc)


def sync_worker_alert(worker):
    try:
        if not isinstance(worker, dict):
            return
        worker_id = _clean_text(worker.get('worker_id') or worker.get('id'), 128)
        if not worker_id:
            return
        _sync_alerts(_worker_alerts(worker), [f'worker:{worker_id}:'], worker_ids={worker_id})
    except Exception as exc:
        logger.exception('worker alert sync failed: %s', exc)


__all__ = ['send_alert_email', 'sync_scan_system_alerts', 'sync_worker_alert']
