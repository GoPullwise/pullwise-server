from __future__ import annotations

import logging
import os
import smtplib
import threading
import time
from email.message import EmailMessage

from . import db, github_auth

logger = logging.getLogger(__name__)

STATE_KEY = 'alert_notifications'
_FALSE_VALUES = {'', '0', 'false', 'no', 'off'}
_SYSTEM_PROBLEM_STATUSES = {'degraded', 'down'}
_WORKER_PROBLEM_STATUSES = {'degraded', 'offline'}
_LOCK = threading.Lock()


def _env(name, default=''):
    return os.environ.get(name, default).strip()


def _env_flag(name, default='false'):
    return _env(name, default).lower() not in _FALSE_VALUES


def _env_int(name, default):
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _clean_header(value):
    return str(value or '').replace('\r', ' ').replace('\n', ' ').strip()[:300]


def _clean_text(value, limit=500):
    return str(value or '').replace('\x00', '').strip()[:limit]


def _recipients():
    raw = _env('PULLWISE_ALERT_EMAIL_TO') or _env('PULLWISE_ADMIN_EMAILS')
    recipients = []
    seen = set()
    for item in raw.split(','):
        email = github_auth.clean_account_email_address(item)
        key = email.lower() if email else ''
        if not key or key in seen:
            continue
        seen.add(key)
        recipients.append(email)
    return recipients


def _sender(recipients):
    sender = (
        _env('PULLWISE_ALERT_EMAIL_FROM')
        or _env('PULLWISE_ALERT_SMTP_FROM')
        or _env('PULLWISE_ALERT_SMTP_USERNAME')
        or (recipients[0] if recipients else '')
        or 'pullwise@example.invalid'
    )
    return github_auth.clean_account_email_address(sender) or 'pullwise@example.invalid'


def send_alert_email(subject, body):
    if not _env_flag('PULLWISE_ALERT_EMAIL_ENABLED', 'false'):
        return False
    recipients = _recipients()
    host = _env('PULLWISE_ALERT_SMTP_HOST')
    if not recipients or not host:
        logger.warning('alert email is enabled but recipient or SMTP host is missing')
        return False
    username = _env('PULLWISE_ALERT_SMTP_USERNAME')
    password = _env('PULLWISE_ALERT_SMTP_PASSWORD')
    use_ssl = _env_flag('PULLWISE_ALERT_SMTP_SSL', 'true')
    starttls = _env_flag('PULLWISE_ALERT_SMTP_STARTTLS', 'false')
    port = _env_int('PULLWISE_ALERT_SMTP_PORT', 465 if use_ssl else 587)
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
    subject = f'Pullwise scan system {status}'
    lines = [
        'Pullwise scan system problem detected.',
        f'Status: {status}',
        f'Online workers: {online} / {total}',
        f'Degraded workers: {degraded}',
        f'Offline workers: {offline}',
        f'Queued jobs: {queued}',
        f'Running jobs: {running}',
    ]
    return {
        f'server:scan-system:{status}': {
            'subject': subject,
            'body': '\n'.join(lines),
            'kind': 'server',
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
            attempted = send_alert_email(alert.get('subject'), alert.get('body'))
            if not attempted:
                continue
            active[key] = {
                'sentAt': current_time,
                'subject': _clean_text(alert.get('subject'), 300),
                'kind': _clean_text(alert.get('kind'), 60),
                'status': _clean_text(alert.get('status'), 60),
            }
            changed = True
        if changed:
            _save_active(active)


def sync_scan_system_alerts(payload, workers):
    try:
        alerts = _system_alert(payload if isinstance(payload, dict) else {})
        for worker in workers or []:
            alerts.update(_worker_alert(worker if isinstance(worker, dict) else {}))
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
        _sync_alerts(_worker_alert(worker), [f'worker:{worker_id}:'])
    except Exception as exc:
        logger.exception('worker alert sync failed: %s', exc)


__all__ = ['send_alert_email', 'sync_scan_system_alerts', 'sync_worker_alert']
