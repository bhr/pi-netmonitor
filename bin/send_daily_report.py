#!/usr/bin/env python3
"""Build a daily HTML report email and pipe it to `msmtp -t` for sending."""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import subprocess
import sys
from email.message import EmailMessage
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

CONF_PATH    = os.environ.get('NETMON_CONF',    '/etc/netmon/netmon.conf')
RAW_DIR      = Path(os.environ.get('NETMON_RAW_DIR', '/var/lib/netmon/raw'))
AGG_DIR      = Path(os.environ.get('NETMON_AGG_DIR', '/var/lib/netmon/aggregates'))
DAILY_CSV    = AGG_DIR / 'daily.csv'
TRAILING_CSV = AGG_DIR / 'trailing5.csv'

METRICS = [
    ('down',   'Download (Mbps)',  False),  # higher_is_better=True if last is True; flipped below
    ('up',     'Upload (Mbps)',    False),
    ('ping',   'Ping (ms)',        True),
    ('jitter', 'Jitter (ms)',      True),
    ('loss',   'Loss (%)',         True),
]
HIGHER_IS_BETTER = {'down': True, 'up': True, 'ping': False, 'jitter': False, 'loss': False}


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
    return (start_local.astimezone(dt.timezone.utc),
            today_local.astimezone(dt.timezone.utc),
            start_local.strftime('%Y-%m-%d'))


def shards_for_range(start_utc: dt.datetime, end_utc: dt.datetime) -> list[Path]:
    months: set[str] = set()
    cur = start_utc
    while cur < end_utc:
        months.add(cur.strftime('%Y-%m'))
        cur += dt.timedelta(hours=1)
    months.add((end_utc - dt.timedelta(seconds=1)).strftime('%Y-%m'))
    return sorted(p for p in (RAW_DIR / f'speedtests-{m}.csv' for m in months) if p.exists())


def read_raw_slice(start_utc: dt.datetime, end_utc: dt.datetime) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    fieldnames: list[str] = []
    for p in shards_for_range(start_utc, end_utc):
        with open(p, newline='') as f:
            reader = csv.DictReader(f)
            if not fieldnames and reader.fieldnames:
                fieldnames = list(reader.fieldnames)
            for r in reader:
                try:
                    ts = dt.datetime.fromisoformat(r['timestamp_iso'].replace('Z', '+00:00'))
                except (ValueError, KeyError):
                    continue
                if start_utc <= ts < end_utc:
                    rows.append(r)
    return rows, fieldnames


def latest_row(path: Path, where: dict | None = None) -> dict | None:
    if not path.exists():
        return None
    rows = list(csv.DictReader(path.open(newline='')))
    if where:
        rows = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
    return rows[-1] if rows else None


def fmt(v) -> str:
    if v is None or v == '':
        return '—'
    try:
        f = float(v)
    except (TypeError, ValueError):
        return escape(str(v))
    if f == int(f):
        return f'{int(f)}'
    return f'{f:.2f}'


def cell_color(metric: str, yday: float | None, trail: float | None,
               threshold: float | None) -> str:
    if yday is None:
        return '#f5f5f5'
    if threshold is not None:
        breached = (yday < threshold) if HIGHER_IS_BETTER[metric] else (yday > threshold)
        if breached:
            return '#ffd6d6'
    if trail is not None and trail > 0:
        ratio = yday / trail
        worse = (ratio < 0.85) if HIGHER_IS_BETTER[metric] else (ratio > 1.15)
        if worse:
            return '#fff2cc'
    return '#e6f4ea'


def f_or_none(v) -> float | None:
    if v in (None, ''):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def render_html(date_label: str, yday: dict, trail: dict, bad_rows: list[dict],
                conf: dict[str, str]) -> str:
    thresholds = {
        'down':   f_or_none(conf.get('THRESH_DOWN_MBPS')),
        'up':     f_or_none(conf.get('THRESH_UP_MBPS')),
        'ping':   f_or_none(conf.get('THRESH_PING_MS')),
        'jitter': f_or_none(conf.get('THRESH_JITTER_MS')),
        'loss':   f_or_none(conf.get('THRESH_LOSS_PCT')),
    }

    style_th = 'padding:6px 10px;text-align:left;background:#f0f0f0;border:1px solid #ccc;'
    style_td = 'padding:6px 10px;border:1px solid #ccc;'

    parts: list[str] = []
    parts.append(f'<h2 style="font-family:sans-serif;">netmon report — {escape(date_label)}</h2>')

    runs = int(yday.get('runs') or 0)
    bad = int(yday.get('bad_event_count') or 0)
    outages = int(yday.get('outages') or 0)
    summary = f'{runs} runs, {bad} bad-event row(s), {outages} outage(s).'
    parts.append(f'<p style="font-family:sans-serif;color:#444;">{escape(summary)}</p>')

    parts.append('<h3 style="font-family:sans-serif;">Yesterday vs. 5-day trailing</h3>')
    parts.append('<table style="border-collapse:collapse;font-family:sans-serif;font-size:13px;">')
    parts.append(
        f'<tr>'
        f'<th style="{style_th}">Metric</th>'
        f'<th style="{style_th}">Yesterday avg</th>'
        f'<th style="{style_th}">Yesterday median</th>'
        f'<th style="{style_th}">Yesterday min</th>'
        f'<th style="{style_th}">Yesterday max</th>'
        f'<th style="{style_th}">Trailing avg</th>'
        f'<th style="{style_th}">Threshold</th>'
        f'</tr>'
    )
    for key, label, _ in METRICS:
        y_avg = f_or_none(yday.get(f'{key}_avg'))
        t_avg = f_or_none(trail.get(f'{key}_avg')) if trail else None
        bg = cell_color(key, y_avg, t_avg, thresholds.get(key))
        parts.append(
            f'<tr>'
            f'<td style="{style_td}"><b>{escape(label)}</b></td>'
            f'<td style="{style_td}background:{bg};">{fmt(yday.get(f"{key}_avg"))}</td>'
            f'<td style="{style_td}">{fmt(yday.get(f"{key}_median"))}</td>'
            f'<td style="{style_td}">{fmt(yday.get(f"{key}_min"))}</td>'
            f'<td style="{style_td}">{fmt(yday.get(f"{key}_max"))}</td>'
            f'<td style="{style_td}">{fmt(trail.get(f"{key}_avg") if trail else None)}</td>'
            f'<td style="{style_td}">{fmt(thresholds.get(key))}</td>'
            f'</tr>'
        )
    parts.append('</table>')

    parts.append('<h3 style="font-family:sans-serif;">Run-status breakdown</h3>')
    parts.append('<table style="border-collapse:collapse;font-family:sans-serif;font-size:13px;">')
    parts.append(
        f'<tr>'
        f'<th style="{style_th}">ok</th>'
        f'<th style="{style_th}">degraded</th>'
        f'<th style="{style_th}">partial</th>'
        f'<th style="{style_th}">outage</th>'
        f'<th style="{style_th}">total</th>'
        f'</tr>'
        f'<tr>'
        f'<td style="{style_td}">{fmt(yday.get("ok"))}</td>'
        f'<td style="{style_td}background:#fff2cc;">{fmt(yday.get("degraded"))}</td>'
        f'<td style="{style_td}background:#ffe0b2;">{fmt(yday.get("partial"))}</td>'
        f'<td style="{style_td}background:#ffd6d6;">{fmt(yday.get("outages"))}</td>'
        f'<td style="{style_td}">{fmt(yday.get("runs"))}</td>'
        f'</tr></table>'
    )

    parts.append('<h3 style="font-family:sans-serif;">Bad-connection events</h3>')
    if not bad_rows:
        parts.append('<p style="font-family:sans-serif;color:#080;">None — every run was within thresholds.</p>')
    else:
        parts.append('<table style="border-collapse:collapse;font-family:sans-serif;font-size:12px;">')
        parts.append(
            f'<tr>'
            f'<th style="{style_th}">Time (UTC)</th>'
            f'<th style="{style_th}">Status</th>'
            f'<th style="{style_th}">Down</th>'
            f'<th style="{style_th}">Up</th>'
            f'<th style="{style_th}">Ping</th>'
            f'<th style="{style_th}">Jitter</th>'
            f'<th style="{style_th}">Loss</th>'
            f'<th style="{style_th}">Note</th>'
            f'</tr>'
        )
        bg_for = {'outage': '#ffd6d6', 'partial': '#ffe0b2', 'degraded': '#fff2cc'}
        for r in bad_rows:
            note = r.get('error') or ''
            url  = r.get('result_url') or ''
            note_cell = escape(note) if note else (
                f'<a href="{escape(url)}">result</a>' if url else ''
            )
            bg = bg_for.get(r.get('status', ''), '#fff')
            parts.append(
                f'<tr>'
                f'<td style="{style_td}">{escape(r.get("timestamp_iso", ""))}</td>'
                f'<td style="{style_td}background:{bg};">{escape(r.get("status", ""))}</td>'
                f'<td style="{style_td}">{fmt(r.get("download_mbps"))}</td>'
                f'<td style="{style_td}">{fmt(r.get("upload_mbps"))}</td>'
                f'<td style="{style_td}">{fmt(r.get("ping_ms"))}</td>'
                f'<td style="{style_td}">{fmt(r.get("jitter_ms"))}</td>'
                f'<td style="{style_td}">{fmt(r.get("loss_pct"))}</td>'
                f'<td style="{style_td}">{note_cell}</td>'
                f'</tr>'
            )
        parts.append('</table>')

    parts.append(
        '<p style="font-family:sans-serif;font-size:11px;color:#888;margin-top:18px;">'
        f'CSV attachments: yesterday raw, full daily aggregates, full 5-day trailing aggregates. '
        f'Generated at {dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")} on this Pi.'
        '</p>'
    )
    return ''.join(parts)


def raw_csv_text(rows: list[dict], fieldnames: list[str]) -> str:
    if not rows:
        return ''
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, '') for k in fieldnames})
    return buf.getvalue()


def attach_csv(msg: EmailMessage, content: str, filename: str) -> None:
    if not content:
        return
    msg.add_attachment(
        content.encode('utf-8'),
        maintype='text', subtype='csv',
        filename=filename,
    )


def build_subject(date_label: str, yday: dict) -> str:
    runs = int(yday.get('runs') or 0)
    if runs == 0:
        return f'[netmon] {date_label} — NO RUNS RECORDED'
    bad = int(yday.get('bad_event_count') or 0)
    outages = int(yday.get('outages') or 0)
    if outages > 0:
        tag = f'{outages} OUTAGE(S)'
    elif bad > 0:
        tag = f'{bad} bad event(s)'
    else:
        tag = 'all good'
    return f'[netmon] {date_label} — {tag}'


def send(msg: EmailMessage) -> None:
    proc = subprocess.run(
        ['msmtp', '-t'],
        input=msg.as_bytes(),
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode('utf-8', 'replace'))
        sys.exit(f'msmtp exited {proc.returncode}')


def main() -> int:
    conf = load_conf(CONF_PATH)
    if not conf.get('REPORT_TO'):
        sys.exit('netmon: REPORT_TO not set in ' + CONF_PATH)
    if conf.get('SMTP_PASS', '').startswith('REPLACE_'):
        sys.exit('netmon: SMTP_PASS still placeholder — edit ' + CONF_PATH)

    tz = resolve_tz(conf)
    start_utc, end_utc, date_label = yesterday_window(tz)

    yday = latest_row(DAILY_CSV, where={'date': date_label}) or {}
    trail = latest_row(TRAILING_CSV) or {}
    raw_rows, raw_fields = read_raw_slice(start_utc, end_utc)
    bad_rows = [r for r in raw_rows if r.get('status') != 'ok']

    html = render_html(date_label, yday, trail, bad_rows, conf)

    msg = EmailMessage()
    msg['From'] = conf.get('SMTP_FROM') or conf.get('SMTP_USER', 'netmon@localhost')
    msg['To'] = conf['REPORT_TO']
    msg['Subject'] = build_subject(date_label, yday)
    msg.set_content('This is the HTML netmon report. Use an HTML-capable mail reader.')
    msg.add_alternative(html, subtype='html')

    attach_csv(msg, raw_csv_text(raw_rows, raw_fields), f'raw-{date_label}.csv')
    if DAILY_CSV.exists():
        attach_csv(msg, DAILY_CSV.read_text(), 'daily.csv')
    if TRAILING_CSV.exists():
        attach_csv(msg, TRAILING_CSV.read_text(), 'trailing5.csv')

    send(msg)
    print(f'send_daily_report: sent report for {date_label} to {conf["REPORT_TO"]}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
