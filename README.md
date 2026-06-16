# netmon — Pi internet quality monitor

Self-contained internet monitor for a Raspberry Pi 5 (Debian 12). Runs an
Ookla speedtest every 5 minutes, captures outages and bad-connection events,
rolls up daily and 5-day-trailing aggregates, and emails an HTML report each
morning with CSV attachments.

## Layout

```
pi/
├── setup.sh                          # idempotent installer, run on the Pi as root
├── bin/
│   ├── run_speedtest.sh              # every 5 min — Ookla + ICMP fallback
│   ├── aggregate_daily.py            # daily roll-up + raw-shard pruning
│   ├── aggregate_trailing.py         # 5-day trailing aggregate
│   └── send_daily_report.py          # HTML email + CSV attachments
├── etc/
│   └── netmon.conf.template          # SMTP creds + thresholds (placeholders)
└── systemd/
    ├── netmon-speedtest.{service,timer}   # OnCalendar=*:0/5
    └── netmon-daily.{service,timer}       # OnCalendar=06:00 daily
```

Deploy targets on the Pi:

| Path                                    | Purpose                                    |
|-----------------------------------------|--------------------------------------------|
| `/opt/netmon/bin/`                      | scripts (mode `0755`)                      |
| `/etc/netmon/netmon.conf`               | config (mode `0640 root:netmon`)           |
| `/var/lib/netmon/.msmtprc`              | SMTP creds (mode `0600 netmon:netmon`)     |
| `/var/lib/netmon/raw/speedtests-YYYY-MM.csv` | one shard per calendar month          |
| `/var/lib/netmon/aggregates/daily.csv`  | one row per day, kept forever              |
| `/var/lib/netmon/aggregates/trailing5.csv` | one row per daily run                   |
| `/var/log/netmon/msmtp.log`             | msmtp send log                             |
| `/etc/systemd/system/netmon-*`          | 4 unit files                               |

## Install

From your workstation:

```bash
scp -r pi benedikt@192.168.5.96:~/
ssh benedikt@192.168.5.96 'sudo bash ~/pi/setup.sh'
```

The installer is idempotent — re-run it any time the conf changes.

After the first run, edit the SMTP credentials and recipient on the Pi:

```bash
ssh benedikt@192.168.5.96 'sudo nano /etc/netmon/netmon.conf'
ssh benedikt@192.168.5.96 'sudo bash ~/pi/setup.sh'   # re-run to regen ~netmon/.msmtprc
```

Look for the line `wrote /var/lib/netmon/.msmtprc (host=..., user=...)` in the
output to confirm the SMTP config was generated.

## Configuration

Everything lives in `/etc/netmon/netmon.conf`. Defaults in
`etc/netmon.conf.template`:

| Key                  | Default                  | Meaning                                  |
|----------------------|--------------------------|------------------------------------------|
| `SMTP_HOST/PORT/USER/PASS/FROM` | smtp.gmail.com:587 | SMTP relay credentials              |
| `REPORT_TO`          | (placeholder)            | recipient of the daily report            |
| `THRESH_DOWN_MBPS`   | `150`                    | minimum acceptable download              |
| `THRESH_UP_MBPS`     | `5`                      | minimum acceptable upload                |
| `THRESH_PING_MS`     | `80`                     | maximum acceptable latency               |
| `THRESH_JITTER_MS`   | `20`                     | maximum acceptable jitter                |
| `THRESH_LOSS_PCT`    | `1.0`                    | maximum acceptable packet loss           |
| `PING_TARGET_PRIMARY/SECONDARY` | 1.1.1.1, 8.8.8.8 | ICMP outage probes                  |
| `RAW_RETENTION_DAYS` | `90`                     | monthly raw shards older than this are pruned |
| `TIMEZONE`           | (system local)           | TZ for "yesterday" / trailing windows    |

A run is classified `ok | degraded | partial | outage`:

| Status     | Meaning                                                          |
|------------|------------------------------------------------------------------|
| `ok`       | speedtest succeeded and every threshold met                      |
| `degraded` | speedtest succeeded but at least one threshold breached          |
| `partial`  | speedtest failed but at least one ICMP probe reachable           |
| `outage`   | speedtest failed and both ICMP probes unreachable                |

Threshold or probe changes take effect on the next 5-minute run — no restart
needed (the Python scripts re-read `netmon.conf` each invocation, and
`run_speedtest.sh` sources it on each run).

## Verification

```bash
# Both timers should show next-run times:
ssh benedikt@192.168.5.96 'systemctl list-timers "netmon-*"'

# Force one speedtest run and inspect the row:
ssh benedikt@192.168.5.96 'sudo systemctl start netmon-speedtest.service'
ssh benedikt@192.168.5.96 "tail -n 1 /var/lib/netmon/raw/speedtests-\$(date -u +%Y-%m).csv"

# Force the full daily pipeline (aggregate → trailing → email):
ssh benedikt@192.168.5.96 'sudo systemctl start netmon-daily.service && sudo journalctl -u netmon-daily -n 30 --no-pager'
```

To exercise the outage path:

```bash
ssh benedikt@192.168.5.96 'sudo iptables -I OUTPUT -d 1.1.1.1 -j DROP; sudo iptables -I OUTPUT -d 8.8.8.8 -j DROP'
ssh benedikt@192.168.5.96 'sudo systemctl start netmon-speedtest.service'
# confirm a status=outage row, then:
ssh benedikt@192.168.5.96 'sudo iptables -D OUTPUT -d 1.1.1.1 -j DROP; sudo iptables -D OUTPUT -d 8.8.8.8 -j DROP'
```

To exercise the threshold path: set `THRESH_DOWN_MBPS=99999` in the conf, run
the script, confirm a `status=degraded` row, revert.

## Performance

At the 5-minute cadence:

- ~288 runs/day × ~200 B/row ≈ **58 KB/day**, **~1.7 MB/month**, **~21 MB/year**.
- Each monthly raw shard is ~8 640 rows — every script reads at most 1–2
  shards per run. No script ever opens a year of data.
- Pruning is `rm` of whole shard files, not a row-by-row rewrite.
- Aggregate files (`daily.csv`, `trailing5.csv`) are ~30 KB/yr each — never
  sharded, kept forever.

## Troubleshooting

**`flock: 9: Bad file descriptor` / `Permission denied` on lock file**
The lock now lives at `/var/lib/netmon/netmon-speedtest.lock` (netmon-owned).
If you see this on an old install, push the latest `bin/run_speedtest.sh` and
remove any stray `/run/netmon-speedtest.lock`.

**`msmtp: account default not found: no configuration file available`**
`netmon` user can't read the msmtp config. Fix by re-running `setup.sh` —
modern installs write `/var/lib/netmon/.msmtprc` (mode `0600 netmon:netmon`)
and remove any stale `/etc/msmtprc`.

**Daily report subject says `NO RUNS RECORDED`**
No raw rows were written for that day. Check
`journalctl -u netmon-speedtest --since '24 hours ago'` and confirm the timer
is enabled (`systemctl status netmon-speedtest.timer`).

**Email arrives but tables show `—` everywhere**
The daily aggregator ran before any speedtest had succeeded. Wait one full
day after install for the first complete report, or trigger
`netmon-daily.service` manually after running a speedtest.

**`speedtest: License not accepted`**
Run once interactively as the netmon user:
```bash
ssh benedikt@192.168.5.96 'sudo -u netmon speedtest --accept-license --accept-gdpr'
```

## Logs

- Per-run speedtest output:  `journalctl -u netmon-speedtest -f`
- Daily pipeline output:     `journalctl -u netmon-daily -f`
- msmtp send log:            `/var/log/netmon/msmtp.log`
