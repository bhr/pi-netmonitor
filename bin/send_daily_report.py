#!/usr/bin/env python3
"""Build a daily HTML report email and pipe it to `msmtp -t` for sending."""

from __future__ import annotations

import argparse
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

# Maps each metric key to its raw-CSV column, for per-threshold pass-rate counting.
METRIC_RAW_FIELD = {
    'down':   'download_mbps',
    'up':     'upload_mbps',
    'ping':   'ping_ms',
    'jitter': 'jitter_ms',
    'loss':   'loss_pct',
}

# Parts of day for the per-period breakdown, as local-hour ranges [start, end).
PARTS_OF_DAY = [
    ('Night',      0,  6),
    ('Morning',    6, 12),
    ('Afternoon', 12, 18),
    ('Evening',   18, 24),
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


def day_window(tz: dt.tzinfo, day: dt.date) -> tuple[dt.datetime, dt.datetime, str]:
    """Local-day window [00:00, next-day 00:00) for `day`, as UTC bounds + label.

    Building both midnights from the wall clock keeps this correct across DST
    transitions (the resulting UTC span is 23/24/25 h as appropriate)."""
    start_local = dt.datetime(day.year, day.month, day.day, tzinfo=tz)
    end_local = start_local + dt.timedelta(days=1)
    return (start_local.astimezone(dt.timezone.utc),
            end_local.astimezone(dt.timezone.utc),
            start_local.strftime('%Y-%m-%d'))


def yesterday_window(tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime, str]:
    yesterday = dt.datetime.now(tz).date() - dt.timedelta(days=1)
    return day_window(tz, yesterday)


def valid_date(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f'invalid date {s!r}; expected YYYY-MM-DD')


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


def metric_breach_bg(metric_key: str, value, thresholds: dict) -> str:
    """Inline 'background:#…;' style when the metric breaches its threshold, else ''."""
    v = f_or_none(value)
    th = thresholds.get(metric_key)
    if v is None or th is None:
        return ''
    breached = (v < th) if HIGHER_IS_BETTER[metric_key] else (v > th)
    return 'background:#ffd6d6;' if breached else ''


def render_html(date_label: str, yday: dict, trail: dict, bad_rows: list[dict],
                conf: dict[str, str], all_rows: list[dict], tz: dt.tzinfo) -> str:
    thresholds = {
        'down':   f_or_none(conf.get('THRESH_DOWN_MBPS')),
        'up':     f_or_none(conf.get('THRESH_UP_MBPS')),
        'ping':   f_or_none(conf.get('THRESH_PING_MS')),
        'jitter': f_or_none(conf.get('THRESH_JITTER_MS')),
        'loss':   f_or_none(conf.get('THRESH_LOSS_PCT')),
    }

    def to_local_str(iso: str) -> str:
        """Render a stored UTC ISO timestamp as 'YYYY-MM-DD HH:MM:SS' in local time."""
        try:
            ts = dt.datetime.fromisoformat((iso or '').replace('Z', '+00:00')).astimezone(tz)
        except (ValueError, TypeError):
            return escape(iso or '')
        return ts.strftime('%Y-%m-%d %H:%M:%S')

    try:
        tz_label = dt.datetime.fromisoformat(date_label).replace(tzinfo=tz, hour=12).tzname() or 'local'
    except ValueError:
        tz_label = 'local'

    style_th = 'padding:6px 10px;text-align:left;background:#f0f0f0;border:1px solid #ccc;'
    style_td = 'padding:6px 10px;border:1px solid #ccc;'

    parts: list[str] = []
    parts.append(f'<h2 style="font-family:sans-serif;">netmon report — {escape(date_label)}</h2>')

    runs = int(yday.get('runs') or 0)
    bad = int(yday.get('bad_event_count') or 0)
    outages = int(yday.get('outages') or 0)
    summary = f'{runs} runs, {bad} bad-event row(s), {outages} outage(s).'
    parts.append(f'<p style="font-family:sans-serif;color:#444;">{escape(summary)}</p>')

    swatch = ('display:inline-block;width:10px;height:10px;border:1px solid #ccc;'
              'vertical-align:middle;margin-right:4px;')
    parts.append(
        '<div style="font-family:sans-serif;font-size:11px;color:#888;'
        'margin:6px 0 16px;line-height:1.7;">'
        f'<b style="color:#666;">Thresholds:</b> '
        f'download ≥ {fmt(thresholds.get("down"))} Mbps · '
        f'upload ≥ {fmt(thresholds.get("up"))} Mbps · '
        f'ping ≤ {fmt(thresholds.get("ping"))} ms · '
        f'jitter ≤ {fmt(thresholds.get("jitter"))} ms · '
        f'loss ≤ {fmt(thresholds.get("loss"))}%'
        '<br>'
        f'<b style="color:#666;">Colors:</b> '
        f'<span style="{swatch}background:#ffd6d6;"></span>outage / threshold breach &nbsp; '
        f'<span style="{swatch}background:#ffe0b2;"></span>partial &nbsp; '
        f'<span style="{swatch}background:#fff2cc;"></span>degraded or below trailing avg &nbsp; '
        f'<span style="{swatch}background:#e6f4ea;"></span>ok'
        '</div>'
    )

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

    runs_total = int(yday.get('runs') or 0)
    ok_n       = int(yday.get('ok') or 0)
    degraded_n = int(yday.get('degraded') or 0)
    partial_n  = int(yday.get('partial') or 0)
    outage_n   = int(yday.get('outages') or 0)

    def pct(n: int, d: int) -> str:
        return f'{n / d * 100:.1f}%' if d > 0 else '—'

    # Status counts, plus each status as a share of all runs. The headline figure
    # is "% ok" = ok runs / all runs.
    parts.append('<table style="border-collapse:collapse;font-family:sans-serif;font-size:13px;">')
    parts.append(
        f'<tr>'
        f'<th style="{style_th}"></th>'
        f'<th style="{style_th}">ok</th>'
        f'<th style="{style_th}">degraded</th>'
        f'<th style="{style_th}">partial</th>'
        f'<th style="{style_th}">outage</th>'
        f'<th style="{style_th}">total</th>'
        f'</tr>'
        f'<tr>'
        f'<td style="{style_th}">count</td>'
        f'<td style="{style_td}">{ok_n}</td>'
        f'<td style="{style_td}background:#fff2cc;">{degraded_n}</td>'
        f'<td style="{style_td}background:#ffe0b2;">{partial_n}</td>'
        f'<td style="{style_td}background:#ffd6d6;">{outage_n}</td>'
        f'<td style="{style_td}">{runs_total}</td>'
        f'</tr>'
        f'<tr>'
        f'<td style="{style_th}">% of total</td>'
        f'<td style="{style_td}background:#e6f4ea;"><b>{pct(ok_n, runs_total)}</b></td>'
        f'<td style="{style_td}">{pct(degraded_n, runs_total)}</td>'
        f'<td style="{style_td}">{pct(partial_n, runs_total)}</td>'
        f'<td style="{style_td}">{pct(outage_n, runs_total)}</td>'
        f'<td style="{style_td}">{pct(runs_total, runs_total)}</td>'
        f'</tr>'
        f'</table>'
    )

    # Per-threshold failure rate: share of all runs whose measured value breached
    # each threshold. partial/outage runs carry no measurement, so they are not
    # counted as a breach of any single threshold. Because one run can breach
    # several thresholds at once, these do not sum to the degraded share above —
    # they isolate which threshold drives the degraded runs.
    th_total = len(all_rows)
    parts.append(
        '<p style="font-family:sans-serif;font-size:12px;color:#444;margin:14px 0 4px;">'
        'Threshold failure rate — runs breaching each threshold &divide; all runs:'
        '</p>'
    )
    parts.append('<table style="border-collapse:collapse;font-family:sans-serif;font-size:13px;">')
    header_cells = ''.join(f'<th style="{style_th}">{escape(label)}</th>' for _, label, _ in METRICS)
    parts.append(f'<tr><th style="{style_th}">Threshold</th>{header_cells}</tr>')

    count_cells: list[str] = []
    pct_cells: list[str] = []
    for key, _, _ in METRICS:
        th = thresholds.get(key)
        field = METRIC_RAW_FIELD[key]
        failed = 0
        for r in all_rows:
            v = f_or_none(r.get(field))
            if v is None or th is None:
                continue
            breached = (v < th) if HIGHER_IS_BETTER[key] else (v > th)
            if breached:
                failed += 1
        count_cells.append(f'<td style="{style_td}">{failed} / {th_total}</td>')
        rate = (failed / th_total) if th_total > 0 else None
        if rate is None:
            bg = '#f5f5f5'
        elif rate <= 0.05:
            bg = '#e6f4ea'
        elif rate <= 0.20:
            bg = '#fff2cc'
        else:
            bg = '#ffd6d6'
        pct_cells.append(f'<td style="{style_td}background:{bg};"><b>{pct(failed, th_total)}</b></td>')
    parts.append(f'<tr><td style="{style_th}">failed</td>{"".join(count_cells)}</tr>')
    parts.append(f'<tr><td style="{style_th}">% failed</td>{"".join(pct_cells)}</tr>')
    parts.append('</table>')

    # Same failure rate, split by part of day (local time). Each run is bucketed
    # by its local hour; within a part the rate is breaches ÷ runs in that part.
    parts.append(
        '<p style="font-family:sans-serif;font-size:12px;color:#444;margin:16px 0 4px;">'
        'Threshold failure rate by part of day — breaches &divide; runs in that part (local time):'
        '</p>'
    )
    legend = ' &nbsp;·&nbsp; '.join(
        f'<b>{escape(name)}</b> {lo:02d}:00–{hi - 1:02d}:59' for name, lo, hi in PARTS_OF_DAY
    )
    parts.append(
        '<div style="font-family:sans-serif;font-size:11px;color:#888;margin:0 0 6px;">'
        f'{legend}</div>'
    )

    period_rows: dict[str, list[dict]] = {name: [] for name, _, _ in PARTS_OF_DAY}
    for r in all_rows:
        try:
            ts = dt.datetime.fromisoformat(
                r['timestamp_iso'].replace('Z', '+00:00')).astimezone(tz)
        except (ValueError, KeyError, TypeError):
            continue
        for name, lo, hi in PARTS_OF_DAY:
            if lo <= ts.hour < hi:
                period_rows[name].append(r)
                break

    parts.append('<table style="border-collapse:collapse;font-family:sans-serif;font-size:13px;">')
    metric_headers = ''.join(f'<th style="{style_th}">{escape(label)}</th>' for _, label, _ in METRICS)
    parts.append(
        f'<tr><th style="{style_th}">Part of day</th>'
        f'<th style="{style_th}">Runs</th>{metric_headers}</tr>'
    )
    for name, _, _ in PARTS_OF_DAY:
        rs = period_rows[name]
        n = len(rs)
        cells: list[str] = []
        for key, _, _ in METRICS:
            th = thresholds.get(key)
            field = METRIC_RAW_FIELD[key]
            failed = 0
            for r in rs:
                v = f_or_none(r.get(field))
                if v is None or th is None:
                    continue
                breached = (v < th) if HIGHER_IS_BETTER[key] else (v > th)
                if breached:
                    failed += 1
            rate = (failed / n) if n > 0 else None
            if rate is None:
                bg = '#f5f5f5'
            elif rate <= 0.05:
                bg = '#e6f4ea'
            elif rate <= 0.20:
                bg = '#fff2cc'
            else:
                bg = '#ffd6d6'
            cells.append(
                f'<td style="{style_td}background:{bg};"><b>{pct(failed, n)}</b>'
                f'<br><span style="font-size:11px;color:#888;">{failed}/{n}</span></td>'
            )
        parts.append(
            f'<tr><td style="{style_th}">{escape(name)}</td>'
            f'<td style="{style_td}">{n}</td>{"".join(cells)}</tr>'
        )
    parts.append('</table>')

    parts.append('<h3 style="font-family:sans-serif;">Bad-connection events</h3>')
    if not bad_rows:
        parts.append('<p style="font-family:sans-serif;color:#080;">None — every run was within thresholds.</p>')
    else:
        parts.append('<table style="border-collapse:collapse;font-family:sans-serif;font-size:12px;">')
        parts.append(
            f'<tr>'
            f'<th style="{style_th}">Time ({escape(tz_label)})</th>'
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
            url  = (r.get('result_url') or '').strip()
            # Only linkify https URLs — defense against a malformed/hostile
            # result_url surfacing as javascript:/data: in a mail client.
            if note:
                note_cell = escape(note)
            elif url.startswith('https://'):
                note_cell = f'<a href="{escape(url)}">result</a>'
            else:
                note_cell = escape(url)
            bg = bg_for.get(r.get('status', ''), '#fff')
            down_bg   = metric_breach_bg('down',   r.get('download_mbps'), thresholds)
            up_bg     = metric_breach_bg('up',     r.get('upload_mbps'),   thresholds)
            ping_bg   = metric_breach_bg('ping',   r.get('ping_ms'),       thresholds)
            jitter_bg = metric_breach_bg('jitter', r.get('jitter_ms'),     thresholds)
            loss_bg   = metric_breach_bg('loss',   r.get('loss_pct'),      thresholds)
            parts.append(
                f'<tr>'
                f'<td style="{style_td}">{to_local_str(r.get("timestamp_iso", ""))}</td>'
                f'<td style="{style_td}background:{bg};">{escape(r.get("status", ""))}</td>'
                f'<td style="{style_td}{down_bg}">{fmt(r.get("download_mbps"))}</td>'
                f'<td style="{style_td}{up_bg}">{fmt(r.get("upload_mbps"))}</td>'
                f'<td style="{style_td}{ping_bg}">{fmt(r.get("ping_ms"))}</td>'
                f'<td style="{style_td}{jitter_bg}">{fmt(r.get("jitter_ms"))}</td>'
                f'<td style="{style_td}{loss_bg}">{fmt(r.get("loss_pct"))}</td>'
                f'<td style="{style_td}">{note_cell}</td>'
                f'</tr>'
            )
        parts.append('</table>')

    parts.append(
        '<p style="font-family:sans-serif;font-size:11px;color:#888;margin-top:18px;">'
        f'CSV attachments: yesterday raw, full daily aggregates, full 5-day trailing aggregates. '
        f'Generated at {dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")} on this Pi.'
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
    parser = argparse.ArgumentParser(
        description='Build and email the netmon daily report (defaults to yesterday).')
    parser.add_argument(
        '--date', type=valid_date, metavar='YYYY-MM-DD',
        help='generate the report for this local day instead of yesterday')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='write the HTML report to stdout instead of emailing it (skips SMTP)')
    args = parser.parse_args()

    conf = load_conf(CONF_PATH)
    if not args.dry_run:
        if not conf.get('REPORT_TO'):
            sys.exit('netmon: REPORT_TO not set in ' + CONF_PATH)
        if conf.get('SMTP_PASS', '').startswith('REPLACE_'):
            sys.exit('netmon: SMTP_PASS still placeholder — edit ' + CONF_PATH)

    tz = resolve_tz(conf)
    if args.date:
        start_utc, end_utc, date_label = day_window(tz, args.date)
        # For an explicit day, use the trailing row whose window ends on it.
        trail = latest_row(TRAILING_CSV, where={'window_end': date_label}) or {}
    else:
        start_utc, end_utc, date_label = yesterday_window(tz)
        trail = latest_row(TRAILING_CSV) or {}

    yday = latest_row(DAILY_CSV, where={'date': date_label}) or {}
    raw_rows, raw_fields = read_raw_slice(start_utc, end_utc)
    bad_rows = [r for r in raw_rows if r.get('status') != 'ok']

    html = render_html(date_label, yday, trail, bad_rows, conf, raw_rows, tz)

    if args.dry_run:
        sys.stdout.write(html)
        print(f'send_daily_report: dry-run for {date_label} '
              f'({len(raw_rows)} raw row(s)) — not sent', file=sys.stderr)
        return 0

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
