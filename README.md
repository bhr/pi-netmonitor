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

Replace `<user>` with your account on the Pi and `<host>` with its hostname or
LAN address (e.g. `pi`, `pi.local`, or `192.168.x.y`).

From your workstation:

```bash
scp -r pi <user>@<host>:~/
ssh <user>@<host> 'sudo bash ~/pi/setup.sh'
```

The installer is idempotent — re-run it any time the conf changes.

After the first run, edit the SMTP credentials and recipient on the Pi:

```bash
ssh <user>@<host> 'sudo nano /etc/netmon/netmon.conf'
ssh <user>@<host> 'sudo bash ~/pi/setup.sh'   # re-run to regen ~netmon/.msmtprc
```

Look for the line `wrote /var/lib/netmon/.msmtprc (host=..., user=...)` in the
output to confirm the SMTP config was generated.

> `setup.sh` adds the official Ookla apt repository via the standard
> `curl … | bash` install snippet from packagecloud.io. If you'd rather not
> pipe a remote script to bash, install the `speedtest` package by hand
> first; the installer skips that step when `speedtest` is already on `PATH`.

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
ssh <user>@<host> 'systemctl list-timers "netmon-*"'

# Force one speedtest run and inspect the row:
ssh <user>@<host> 'sudo systemctl start netmon-speedtest.service'
ssh <user>@<host> "tail -n 1 /var/lib/netmon/raw/speedtests-\$(date -u +%Y-%m).csv"

# Force the full daily pipeline (aggregate → trailing → email):
ssh <user>@<host> 'sudo systemctl start netmon-daily.service && sudo journalctl -u netmon-daily -n 30 --no-pager'
```

To exercise the outage path:

```bash
ssh <user>@<host> 'sudo iptables -I OUTPUT -d 1.1.1.1 -j DROP; sudo iptables -I OUTPUT -d 8.8.8.8 -j DROP'
ssh <user>@<host> 'sudo systemctl start netmon-speedtest.service'
# confirm a status=outage row, then:
ssh <user>@<host> 'sudo iptables -D OUTPUT -d 1.1.1.1 -j DROP; sudo iptables -D OUTPUT -d 8.8.8.8 -j DROP'
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
ssh <user>@<host> 'sudo -u netmon speedtest --accept-license --accept-gdpr'
```

## Logs

- Per-run speedtest output:  `journalctl -u netmon-speedtest -f`
- Daily pipeline output:     `journalctl -u netmon-daily -f`
- msmtp send log:            `/var/log/netmon/msmtp.log`

## Security model

- `/etc/netmon/netmon.conf` is the only secret-bearing file in this repo's
  install path. It is mode `0640 root:netmon` — only `root` can write it,
  only `root` and processes running as `netmon` can read it.
- The conf is **sourced as bash** by `setup.sh` (running as root) and
  `run_speedtest.sh` (running as `netmon`). Treat it like an executable
  shell script — anything you put in the value of a variable will be
  evaluated by the shell. The Python scripts use a separate regex parser
  and do not eval the conf.
- The SMTP password lives in `/var/lib/netmon/.msmtprc`, mode `0600
  netmon:netmon`. It is generated from the conf by `setup.sh`; the password
  never leaves the Pi.
- The speedtest binary is closed-source from Ookla; it talks to
  `speedtest.net` servers over HTTPS. If you want to avoid that, swap
  `run_speedtest.sh` for an iperf3-against-your-own-target version — the
  CSV columns will still match.
- `setup.sh` adds the Ookla apt repo using their official `curl … | bash`
  install snippet, which is the standard antipattern for installing a
  third-party Debian repo. The snippet is hosted by packagecloud.io. If
  you'd rather not pipe a remote script to bash, install the `speedtest`
  package by hand first; the installer skips that step when `speedtest` is
  already on `PATH`.

## License

This is a personal-project monitor; treat the code in this repo as MIT/0-BSD
unless a `LICENSE` file says otherwise. The Ookla speedtest CLI it invokes
has its own license, which it asks you to accept on first run.
