#!/usr/bin/env python3
"""Aggregate yesterday's speedtest runs into one row of daily.csv, then prune old shards."""

from __future__ import annotations

import csv
import datetime as dt
import os
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

CONF_PATH      = os.environ.get('NETMON_CONF', '/etc/netmon/netmon.conf')
RAW_DIR        = Path(os.environ.get('NETMON_RAW_DIR',        '/var/lib/netmon/raw'))
AGG_DIR        = Path(os.environ.get('NETMON_AGG_DIR',        '/var/lib/netmon/aggregates'))
DAILY_CSV      = AGG_DIR / 'daily.csv'

DAILY_COLUMNS = [
    'date', 'runs', 'ok', 'degraded', 'partial', 'outages', 'bad_event_count',
    'down_avg', 'down_median', 'down_min', 'down_max',
    'up_avg', 'up_median', 'up_min', 'up_max',
    'ping_avg', 'ping_median', 'ping_min', 'ping_max',
    'jitter_avg', 'jitter_median', 'jitter_min', 'jitter_max',
    'loss_avg', 'loss_median', 'loss_min', 'loss_max',
    'worst_events',
]

NUMERIC = [
    ('download_mbps', 'down'),
    ('upload_mbps',   'up'),
    ('ping_ms',       'ping'),
    ('jitter_ms',     'jitter'),
    ('loss_pct',      'loss'),
]


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


def yesterday_window(tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime, str]:
    now_local = dt.datetime.now(tz)
    today_local = dt.datetime(now_local.year, now_local.month, now_local.day, tzinfo=tz)
    start_local = today_local - dt.timedelta(days=1)
    label = start_local.strftime('%Y-%m-%d')
    return (start_local.astimezone(dt.timezone.utc),
            today_local.astimezone(dt.timezone.utc),
            label)


def shards_for_range(start_utc: dt.datetime, end_utc: dt.datetime) -> list[Path]:
    months: set[str] = set()
    cur = start_utc
    step = dt.timedelta(hours=1)
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


def compute_stats(rows: list[dict], date_label: str) -> dict:
    by_status = {'ok': 0, 'degraded': 0, 'partial': 0, 'outage': 0}
    samples: dict[str, list[float]] = {f: [] for f, _ in NUMERIC}
    bad: list[tuple] = []

    for r in rows:
        s = r.get('status', '')
        by_status[s] = by_status.get(s, 0) + 1
        if s == 'ok' or s == 'degraded':
            # 'degraded' rows still have valid measurements — include them in stats.
            for field, _ in NUMERIC:
                v = to_float(r.get(field))
                if v is not None:
                    samples[field].append(v)
        if s != 'ok':
            bad.append((
                r.get('timestamp_iso', ''),
                s,
                to_float(r.get('download_mbps')),
                to_float(r.get('upload_mbps')),
                to_float(r.get('ping_ms')),
            ))

    out: dict = {
        'date': date_label,
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

    severity = {'outage': 3, 'partial': 2, 'degraded': 1}
    bad.sort(key=lambda e: (-severity.get(e[1], 0), e[2] if e[2] is not None else 0))
    out['worst_events'] = '|'.join(f'{e[0]}@{e[1]}' for e in bad[:5])
    return out


def write_daily(row: dict) -> None:
    AGG_DIR.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if DAILY_CSV.exists():
        with open(DAILY_CSV, newline='') as f:
            existing = [r for r in csv.DictReader(f) if r.get('date') != row['date']]
    existing.append(row)
    existing.sort(key=lambda r: r['date'])

    tmp = DAILY_CSV.with_suffix('.csv.tmp')
    with open(tmp, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=DAILY_COLUMNS)
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k, '') for k in DAILY_COLUMNS})
    os.replace(tmp, DAILY_CSV)


def prune_shards(retention_days: int) -> int:
    if retention_days <= 0 or not RAW_DIR.exists():
        return 0
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).strftime('%Y-%m')
    removed = 0
    for p in RAW_DIR.iterdir():
        name = p.name
        if not (name.startswith('speedtests-') and name.endswith('.csv')):
            continue
        month = name[len('speedtests-'):-len('.csv')]
        if len(month) == 7 and month < cutoff:
            p.unlink()
            removed += 1
    return removed


def main() -> int:
    conf = load_conf(CONF_PATH)
    tz = resolve_tz(conf)
    start_utc, end_utc, label = yesterday_window(tz)

    shards = shards_for_range(start_utc, end_utc)
    rows = read_rows(shards, start_utc, end_utc)

    if not rows:
        print(f'aggregate_daily: no rows for {label} in {[p.name for p in shards]}', file=sys.stderr)

    stats_row = compute_stats(rows, label)
    write_daily(stats_row)

    try:
        retention = int(conf.get('RAW_RETENTION_DAYS', '90'))
    except ValueError:
        retention = 90
    removed = prune_shards(retention)
    print(f'aggregate_daily: wrote {label} ({stats_row["runs"]} runs, '
          f'{stats_row["bad_event_count"]} bad); pruned {removed} old shard(s)',
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
