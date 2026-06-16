#!/usr/bin/env python3
"""Append a 5-day trailing aggregate row to trailing5.csv.

Window = the 5 *full* days ending at midnight last night (local TZ).
Each invocation appends one row, so the file is a time series of how trailing
quality is evolving.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

CONF_PATH    = os.environ.get('NETMON_CONF',    '/etc/netmon/netmon.conf')
RAW_DIR      = Path(os.environ.get('NETMON_RAW_DIR', '/var/lib/netmon/raw'))
AGG_DIR      = Path(os.environ.get('NETMON_AGG_DIR', '/var/lib/netmon/aggregates'))
TRAILING_CSV = AGG_DIR / 'trailing5.csv'

TRAILING_COLUMNS = [
    'run_at_utc', 'window_start', 'window_end', 'days', 'runs',
    'ok', 'degraded', 'partial', 'outages', 'bad_event_count',
    'down_avg', 'down_median', 'down_min', 'down_max',
    'up_avg', 'up_median', 'up_min', 'up_max',
    'ping_avg', 'ping_median', 'ping_min', 'ping_max',
    'jitter_avg', 'jitter_median', 'jitter_min', 'jitter_max',
    'loss_avg', 'loss_median', 'loss_min', 'loss_max',
]

NUMERIC = [
    ('download_mbps', 'down'),
    ('upload_mbps',   'up'),
    ('ping_ms',       'ping'),
    ('jitter_ms',     'jitter'),
    ('loss_pct',      'loss'),
]

WINDOW_DAYS = 5


def load_conf(path: str) -> dict[str, str]:
    conf: dict[str, str] = {}
    if not os.path.exists(path):
        return conf
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            conf[k] = v
    return conf


def resolve_tz(conf: dict[str, str]) -> dt.tzinfo:
    name = conf.get('TIMEZONE', '').strip()
    if name:
        return ZoneInfo(name)
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def trailing_window(tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime, str, str]:
    now_local = dt.datetime.now(tz)
    today_local = dt.datetime(now_local.year, now_local.month, now_local.day, tzinfo=tz)
    end_local   = today_local
    start_local = today_local - dt.timedelta(days=WINDOW_DAYS)
    return (start_local.astimezone(dt.timezone.utc),
            end_local.astimezone(dt.timezone.utc),
            start_local.strftime('%Y-%m-%d'),
            (end_local - dt.timedelta(days=1)).strftime('%Y-%m-%d'))


def shards_for_range(start_utc: dt.datetime, end_utc: dt.datetime) -> list[Path]:
    months: set[str] = set()
    cur = start_utc
    step = dt.timedelta(hours=6)
    while cur < end_utc:
        months.add(cur.strftime('%Y-%m'))
        cur += step
    months.add((end_utc - dt.timedelta(seconds=1)).strftime('%Y-%m'))
    out = []
    for m in sorted(months):
        p = RAW_DIR / f'speedtests-{m}.csv'
        if p.exists():
            out.append(p)
    return out


def read_rows(paths: list[Path], start_utc: dt.datetime, end_utc: dt.datetime) -> list[dict]:
    rows = []
    for p in paths:
        with open(p, newline='') as f:
            for r in csv.DictReader(f):
                try:
                    ts = dt.datetime.fromisoformat(r['timestamp_iso'].replace('Z', '+00:00'))
                except (ValueError, KeyError):
                    continue
                if start_utc <= ts < end_utc:
                    rows.append(r)
    return rows


def to_float(s: str | None) -> float | None:
    if s is None or s == '':
        return None
    try:
        return float(s)
    except ValueError:
        return None


def compute(rows: list[dict], window_start: str, window_end: str) -> dict:
    by_status = {'ok': 0, 'degraded': 0, 'partial': 0, 'outage': 0}
    samples: dict[str, list[float]] = {f: [] for f, _ in NUMERIC}

    for r in rows:
        s = r.get('status', '')
        by_status[s] = by_status.get(s, 0) + 1
        if s in ('ok', 'degraded'):
            for field, _ in NUMERIC:
                v = to_float(r.get(field))
                if v is not None:
                    samples[field].append(v)

    out: dict = {
        'run_at_utc': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'window_start': window_start,
        'window_end': window_end,
        'days': WINDOW_DAYS,
        'runs': sum(by_status.values()),
        'ok': by_status['ok'],
        'degraded': by_status['degraded'],
        'partial': by_status['partial'],
        'outages': by_status['outage'],
    }
    out['bad_event_count'] = out['degraded'] + out['partial'] + out['outages']

    for field, prefix in NUMERIC:
        vs = samples[field]
        if vs:
            out[f'{prefix}_avg']    = round(sum(vs) / len(vs), 2)
            out[f'{prefix}_median'] = round(statistics.median(vs), 2)
            out[f'{prefix}_min']    = round(min(vs), 2)
            out[f'{prefix}_max']    = round(max(vs), 2)
        else:
            for sfx in ('avg', 'median', 'min', 'max'):
                out[f'{prefix}_{sfx}'] = ''
    return out


def append_trailing(row: dict) -> None:
    AGG_DIR.mkdir(parents=True, exist_ok=True)
    new = not TRAILING_CSV.exists() or TRAILING_CSV.stat().st_size == 0
    with open(TRAILING_CSV, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=TRAILING_COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, '') for k in TRAILING_COLUMNS})


def main() -> int:
    conf = load_conf(CONF_PATH)
    tz = resolve_tz(conf)
    start_utc, end_utc, w_start, w_end = trailing_window(tz)

    shards = shards_for_range(start_utc, end_utc)
    rows = read_rows(shards, start_utc, end_utc)
    stats = compute(rows, w_start, w_end)
    append_trailing(stats)

    print(f'aggregate_trailing: window {w_start}..{w_end}, '
          f'{stats["runs"]} runs, {stats["bad_event_count"]} bad',
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
