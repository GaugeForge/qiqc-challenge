#!/usr/bin/env python3
"""Read-only aggregate quota status; never requests a model or mutates a service."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import stat
import urllib.error
import urllib.request


def private_bytes(path, maximum):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('private_path_required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o037 or info.st_size > maximum:
            raise ValueError('private_file_required')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('private_file_size_limit')
        return data
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def provider_summary(profile):
    # The fixed URL prevents organizer profile metadata from redirecting this
    # credential-bearing request. Response bodies are never included in errors.
    if profile.get('base_url', '').rstrip('/') != 'https://openrouter.ai/api/v1':
        return {'state': 'unsupported_provider_for_account_query'}
    key = private_bytes(profile['key_file'], 1024).decode().strip()
    if not 12 <= len(key) <= 512 or '\n' in key or '\r' in key:
        raise ValueError('invalid_key_reference')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request('https://openrouter.ai/api/v1/key', headers={
        'Authorization': 'Bearer ' + key, 'User-Agent': 'qiqc-budget-observer/1'})
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('provider_response_size_limit')
        parsed = json.loads(raw)
        values = parsed.get('data', {}).get('free_model_daily_requests', {})
        safe = {k: values.get(k) for k in ('used', 'limit', 'remaining')}
        if any(type(v) is not int or v < 0 for v in safe.values()):
            return {'state': 'free_daily_counter_unavailable'}
        return {'state': 'queried', 'free_model_daily_requests': safe}
    except urllib.error.HTTPError as error:
        return {'state': 'account_query_failed', 'http_status': error.code}
    except Exception:
        return {'state': 'account_query_unavailable'}


def status(profile, database, query_provider=False):
    group = profile.get('quota_group')
    rpm, daily = profile.get('requests_per_minute'), profile.get('requests_per_day')
    if not isinstance(group, str) or not group or type(rpm) is not int or type(daily) is not int or rpm < 1 or daily < 1:
        raise ValueError('profile_quota_fields_required')
    path = Path(database).absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError('existing_regular_database_required')
    now = dt.datetime.now(dt.timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_pacing_admissions'").fetchone()
        if table:
            used = conn.execute('SELECT COUNT(*) FROM provider_pacing_admissions WHERE quota_group=? AND started_ns>=?', (group, int(day * 1e9))).fetchone()[0]
            recent = conn.execute('SELECT COUNT(*) FROM provider_pacing_admissions WHERE quota_group=? AND started_ns>?', (group, int((now.timestamp() - 60) * 1e9))).fetchone()[0]
        else:
            used, recent = 0, 0
    result = {'schema': 'qiqc-free-budget-status/v1', 'checked_at': now.isoformat(),
              'model': profile.get('model'), 'quota_group': group,
              'local_reservations_today': used, 'local_reservations_last_minute': recent,
              'local_daily_cap': daily, 'local_rpm': rpm,
              'local_daily_remaining': max(0, daily - used),
              'reservation_is_not_provider_usage': True,
              'provider': provider_summary(profile) if query_provider else {'state': 'not_queried'}}
    remote = result['provider'].get('free_model_daily_requests')
    if remote:
        result['remaining_upper_bound'] = min(result['local_daily_remaining'], remote['remaining'])
    result['remaining_is_no_guarantee'] = 'Other clients and later retries may consume allowance; models and keys on one account do not reset it.'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--query-provider', action='store_true', help='GET account counter only; no model call')
    args = parser.parse_args()
    try:
        profile = json.loads(private_bytes(args.profile, 16384))
        result = status(profile, args.database, args.query_provider)
    except Exception:
        result = {'schema': 'qiqc-free-budget-status/v1', 'state': 'local_budget_status_unavailable'}
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get('state') == 'local_budget_status_unavailable' else 0


if __name__ == '__main__':
    raise SystemExit(main())
