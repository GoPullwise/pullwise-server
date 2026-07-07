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
    subject = f'Pullwise worker Codex quota {status}: {name}'
    lines = [
        'Pullwise worker Codex quota warning detected.',
        f'Worker: {name} ({worker_id})',
        f'Status: {status}',
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
        f'worker:{worker_id}:codex-quota:{status}': {
            'subject': subject,
            'body': '\n'.join(lines),
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
    subject = f'Pullwise worker {status}: {name}'
    lines = [
        'Pullwise worker problem detected.',
        f'Worker: {name} ({worker_id})',
        f'Status: {status}',
        f'Provider: {provider}',
        f'Doctor status: {doctor_status}',
        f'Codex ready: {codex_ready}',
        f'Last heartbeat: {last_heartbeat}',
    ]
    last_error = _clean_text(worker.get('last_error'))
    if last_error:
        lines.append(f'Last error: {last_error}')
    return {
        f'worker:{worker_id}:{status}': {
            'subject': subject,
            'body': '\n'.join(lines),
            'kind': 'worker',
            'status': status,
        }
    }



def _worker_alerts(worker):
    alerts = _worker_alert(worker)
    alerts.update(_worker_quota_alert(worker))
    return alerts

def _sync_alerts(alerts, clear_prefixes):
    with _LOCK:
        active = _load_active()
        changed = False
        for key in list(active):
            if any(key.startswith(prefix) for prefix in clear_prefixes) and key not in alerts:
                active.pop(key, None)
                changed = True
        current_time = int(time.time())
        for key, alert in alerts.items():
            if key in active:
                continue
            email_delivered = send_alert_email(alert.get('subject'), alert.get('body'))
            entry = {
                'attemptedAt': current_time,
                'subject': _clean_text(alert.get('subject'), 300),
                'kind': _clean_text(alert.get('kind'), 60),
                'status': _clean_text(alert.get('status'), 60),
                'emailDelivered': bool(email_delivered),
            }
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
            alerts.update(_worker_alerts(worker if isinstance(worker, dict) else {}))
        _sync_alerts(alerts, ['server:scan-system:', 'worker:'])
    except Exception as exc:
        logger.exception('scan system alert sync failed: %s', exc)


def sync_worker_alert(worker):
    try:
        if not isinstance(worker, dict):
            return
        worker_id = _clean_text(worker.get('worker_id') or worker.get('id'), 128)
        if not worker_id:
            return
        _sync_alerts(_worker_alerts(worker), [f'worker:{worker_id}:'])
    except Exception as exc:
        logger.exception('worker alert sync failed: %s', exc)


__all__ = ['send_alert_email', 'sync_scan_system_alerts', 'sync_worker_alert']

