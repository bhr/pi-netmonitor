#!/usr/bin/env bash
# Runs every 5 minutes via netmon-speedtest.timer.
# Always writes one CSV row, even on failure, so outages are recorded.

set -u

CONF="${NETMON_CONF:-/etc/netmon/netmon.conf}"
DATA_DIR="${NETMON_DATA_DIR:-/var/lib/netmon/raw}"
LOCK="${NETMON_LOCK:-/var/lib/netmon/netmon-speedtest.lock}"

THRESH_DOWN_MBPS=150
THRESH_UP_MBPS=5
THRESH_PING_MS=80
THRESH_JITTER_MS=20
THRESH_LOSS_PCT=1.0
PING_TARGET_PRIMARY="1.1.1.1"
PING_TARGET_SECONDARY="8.8.8.8"

if [[ -r "$CONF" ]]; then
    set -a; source "$CONF"; set +a
fi

exec 9>"$LOCK" || { echo "netmon: cannot open lock file $LOCK" >&2; exit 1; }
if ! flock -n 9; then
    echo "netmon: previous run still in progress, skipping" >&2
    exit 0
fi

mkdir -p "$DATA_DIR"

month="$(date -u '+%Y-%m')"
out="$DATA_DIR/speedtests-$month.csv"

probe_ping() {
    local target="$1" output rtt
    if output="$(ping -c 5 -W 2 -q "$target" 2>/dev/null)"; then
        rtt="$(awk -F'/' '/rtt|round-trip/ {print $5}' <<<"$output")"
        printf 'ok %s\n' "${rtt:-}"
    else
        printf 'fail \n'
    fi
}

read -r p1_state p1_rtt < <(probe_ping "$PING_TARGET_PRIMARY")
read -r p2_state p2_rtt < <(probe_ping "$PING_TARGET_SECONDARY")
p1_ok=0; [[ "$p1_state" == "ok" ]] && p1_ok=1
p2_ok=0; [[ "$p2_state" == "ok" ]] && p2_ok=1

st_status="fail"
st_json=""
st_err=""
st_err_file="$(mktemp)"
trap 'rm -f "$st_err_file"' EXIT

if command -v speedtest >/dev/null 2>&1; then
    if st_json="$(timeout 90 speedtest --accept-license --accept-gdpr --format=json 2>"$st_err_file")"; then
        st_status="ok"
    else
        st_err="$(tr '\n' ' ' <"$st_err_file" | head -c 240)"
    fi
else
    st_err="speedtest CLI not installed"
fi

download_mbps=""; upload_mbps=""; ping_ms=""; jitter_ms=""; loss_pct=""
server_id=""; server_name=""; isp=""; result_url=""

if [[ "$st_status" == "ok" ]]; then
    download_mbps="$(jq -r '(.download.bandwidth // 0) * 8 / 1e6 | (. * 100 | floor) / 100' <<<"$st_json")"
    upload_mbps="$(jq -r '(.upload.bandwidth   // 0) * 8 / 1e6 | (. * 100 | floor) / 100' <<<"$st_json")"
    ping_ms="$(jq -r     '.ping.latency // empty'                                          <<<"$st_json")"
    jitter_ms="$(jq -r   '.ping.jitter  // empty'                                          <<<"$st_json")"
    loss_pct="$(jq -r    '.packetLoss   // 0'                                              <<<"$st_json")"
    server_id="$(jq -r   '.server.id    // empty'                                          <<<"$st_json")"
    server_name="$(jq -r '.server.name  // empty'                                          <<<"$st_json")"
    isp="$(jq -r         '.isp          // empty'                                          <<<"$st_json")"
    result_url="$(jq -r  '.result.url   // empty'                                          <<<"$st_json")"
fi

gt() { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a+0 > b+0)}'; }
lt() { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a+0 < b+0)}'; }

if [[ "$st_status" == "fail" ]]; then
    if (( p1_ok == 0 && p2_ok == 0 )); then
        status="outage"
    else
        status="partial"
    fi
else
    status="ok"
    if   lt "$download_mbps" "$THRESH_DOWN_MBPS"; then status="degraded"
    elif lt "$upload_mbps"   "$THRESH_UP_MBPS";   then status="degraded"
    elif gt "$ping_ms"       "$THRESH_PING_MS";   then status="degraded"
    elif gt "$jitter_ms"     "$THRESH_JITTER_MS"; then status="degraded"
    elif gt "$loss_pct"      "$THRESH_LOSS_PCT";  then status="degraded"
    fi
fi

csv_quote() {
    local s="${1//\"/\"\"}"
    printf '"%s"' "$s"
}

ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

row="$(csv_quote "$ts"),"
row+="$(csv_quote "$status"),"
row+="${download_mbps},${upload_mbps},${ping_ms},${jitter_ms},${loss_pct},"
row+="$(csv_quote "$PING_TARGET_PRIMARY"),${p1_ok},${p1_rtt},"
row+="$(csv_quote "$PING_TARGET_SECONDARY"),${p2_ok},${p2_rtt},"
row+="$(csv_quote "$server_id"),$(csv_quote "$server_name"),$(csv_quote "$isp"),"
row+="$(csv_quote "$result_url"),$(csv_quote "$st_err")"

if [[ ! -s "$out" ]]; then
    hdr='timestamp_iso,status,download_mbps,upload_mbps,ping_ms,jitter_ms,loss_pct,ping_primary_addr,ping_primary_ok,ping_primary_rtt_ms,ping_secondary_addr,ping_secondary_ok,ping_secondary_rtt_ms,server_id,server_name,isp,result_url,error'
    tmp="$(mktemp "$DATA_DIR/.speedtests-XXXXXX")"
    printf '%s\n' "$hdr" > "$tmp"
    mv "$tmp" "$out"
fi

printf '%s\n' "$row" >> "$out"
echo "netmon: status=$status down=${download_mbps:-?} up=${upload_mbps:-?} ping=${ping_ms:-?}" >&2
