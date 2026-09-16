#!/usr/bin/env bash
# Host metrics collector for the ops console.
#
# Runs ON THE HOST under its own systemd timer, writes a JSON snapshot, and
# that is the console's only view of the machine. The console is a container:
# df and /proc inside it describe the container, not the VPS. Rather than hand
# the web app a Docker socket or a shell — which the plan forbids outright —
# the privileged half lives here, in a script the web app cannot invoke.
#
# Writes atomically (temp file + mv) so the console never reads a half-written
# file. Install: see ops/systemd/ops-metrics.{service,timer}

set -euo pipefail

OUT_DIR="${OUT_DIR:-/var/ops-metrics}"
OUT="${OUT_DIR}/metrics.json"
TMP="${OUT}.tmp"
BACKUP_DIR="${BACKUP_DIR:-/home/ubuntu/backups}"

mkdir -p "$OUT_DIR"

# --- disk ------------------------------------------------------------------
read -r _ SIZE USED _ PCT _ < <(df -h / | tail -1)
PCT_NUM="${PCT%\%}"

# Component breakdown — the thing that answers "what is using the disk"
# without anyone SSHing in. du can be slow, so each is capped to one level.
breakdown_json() {
  local first=1
  emit() {
    local name="$1" size="$2"
    [ -z "$size" ] && return
    [ $first -eq 0 ] && printf ','
    first=0
    printf '{"name":"%s","size":"%s"}' "$name" "$size"
  }
  printf '['
  emit "Docker (images, containers, volumes)" "$(docker system df --format '{{.Size}}' 2>/dev/null | head -1 || true)"
  emit "Postgres volume"  "$(du -sh /var/lib/docker/volumes/core_pgdata   2>/dev/null | cut -f1 || true)"
  emit "MinIO volume"     "$(du -sh /var/lib/docker/volumes/core_miniodata 2>/dev/null | cut -f1 || true)"
  emit "Backups"          "$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1 || true)"
  emit "Journal logs"     "$(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMG]' | tail -1 || true)"
  emit "Home directory"   "$(du -sh /home/ubuntu 2>/dev/null | cut -f1 || true)"
  printf ']'
}

# --- memory / load ---------------------------------------------------------
MEM_TOTAL=$(free -m | awk '/^Mem:/{print $2}')
MEM_USED=$(free -m  | awk '/^Mem:/{print $3}')
MEM_PCT=$(( MEM_TOTAL > 0 ? MEM_USED * 100 / MEM_TOTAL : 0 ))
read -r L1 L5 L15 _ < /proc/loadavg
CPUS=$(nproc)

# --- containers ------------------------------------------------------------
containers_json() {
  local first=1
  printf '['
  while IFS='|' read -r name state status; do
    [ -z "$name" ] && continue
    [ $first -eq 0 ] && printf ','
    first=0
    printf '{"name":"%s","state":"%s","status":"%s","mem":null}' \
      "${name//\"/}" "${state//\"/}" "${status//\"/}"
  done < <(docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null || true)
  printf ']'
}

# --- backups ---------------------------------------------------------------
backup_json() {
  if [ ! -d "$BACKUP_DIR" ]; then
    printf '{"latest":null,"age_hours":null,"count":0,"total_size":"0"}'
    return
  fi
  local latest count size age_h epoch now
  latest=$(ls -1t "$BACKUP_DIR" 2>/dev/null | head -1 || true)
  count=$(ls -1 "$BACKUP_DIR" 2>/dev/null | wc -l | tr -d ' ')
  size=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1 || echo "0")
  if [ -n "$latest" ]; then
    epoch=$(stat -c %Y "$BACKUP_DIR/$latest" 2>/dev/null || echo 0)
    now=$(date +%s)
    age_h=$(( (now - epoch) / 3600 ))
  else
    age_h=null
  fi
  printf '{"latest":"%s","age_hours":%s,"count":%s,"total_size":"%s"}' \
    "$latest" "${age_h:-null}" "$count" "$size"
}

cat > "$TMP" <<JSON
{
  "collected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "hostname": "$(hostname)",
  "uptime": "$(uptime -p 2>/dev/null || echo unknown)",
  "cpu_count": ${CPUS},
  "load": {"1m": ${L1}, "5m": ${L5}, "15m": ${L15}},
  "memory": {"total_mb": ${MEM_TOTAL}, "used_mb": ${MEM_USED}, "use_percent": ${MEM_PCT}},
  "disk": {"size": "${SIZE}", "used": "${USED}", "use_percent": ${PCT_NUM},
           "breakdown": $(breakdown_json)},
  "containers": $(containers_json),
  "backup": $(backup_json)
}
JSON

# Validate before publishing — a malformed snapshot should never replace a
# good one.
if command -v python3 >/dev/null 2>&1; then
  python3 -c "import json,sys; json.load(open('$TMP'))" || {
    echo "collect_metrics: produced invalid JSON, keeping previous snapshot" >&2
    rm -f "$TMP"; exit 1
  }
fi

mv "$TMP" "$OUT"
chmod 0644 "$OUT"
echo "collect_metrics: wrote $OUT"
