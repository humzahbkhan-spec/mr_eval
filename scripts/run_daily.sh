#!/bin/bash
# Predicting Tyler — daily pipeline runner (launchd, on your Mac).
#
# Runs on the Mac because Substack (Cloudflare) blocks the datacenter IPs that
# GitHub Actions runners use — a residential IP polls all 538 feeds fine, a
# GH runner gets ~90% 403s. See DECISIONS.md D-39.
#
# It runs the pipeline, publishes the updated DB as the `data-latest` GitHub
# Release asset the dashboard reads, and nudges Streamlit Cloud to redeploy.
#
#   bash scripts/run_daily.sh            # full run (ranks — costs ~$2-3)
#   bash scripts/run_daily.sh --no-rank  # free test of everything but ranking
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

PROJECT="/Users/humzahkhan/Projects/mr_eval"
cd "$PROJECT" || exit 1
echo "===== daily run $(date -u +%FT%TZ) ====="

# --- retry-safe guard -----------------------------------------------------
# launchd fires this several times each morning (09:00, 09:10, then every 30
# min — see the plist) so a run that misses because wifi wasn't up yet, or
# because credits were briefly exhausted, gets retried instead of losing the
# day. To make repeated fires safe, we must do NOTHING once today already
# produced a good run — otherwise every retry would re-rank and re-charge.
#
# "Done today" = a success marker stamped with today's date. The marker is
# written at the end ONLY when this run actually stored predictions, so an
# offline-skip or a credits-out 402 (both leave 0 predictions) do NOT count as
# done and the next fire retries. A skipped day is an honest gap; a degraded
# day is not.
TODAY="$(date +%F)"
MARKER="data/.last_success_date"
if [ "$(cat "$MARKER" 2>/dev/null)" = "$TODAY" ]; then
  echo "[guard] already completed a successful run today ($TODAY) — nothing to do."
  exit 0
fi

# Wait for real network before doing anything. A run started before wifi has
# associated gets ~90% DNS failures (Errno 8), ranks a crippled pool, and fails
# to push. Poll up to ~5 min (kept short so one fire's wait finishes before the
# next scheduled fire); if still offline, exit without marking done, so the next
# fire retries.
online() { /usr/bin/python3 -c "import socket; socket.setdefaulttimeout(5); socket.gethostbyname('marginalrevolution.com')" 2>/dev/null; }
for i in $(seq 1 30); do
  online && break
  echo "[wait] no network yet (try $i/30), sleeping 10s…"
  sleep 10
done
if ! online; then
  echo "[wait] still offline after ~5 min — skipping this fire; a later fire will retry."
  exit 0
fi
echo "[wait] network up — proceeding."

# Secrets (OPENROUTER_API_KEY) from .env
set -a; [ -f .env ] && source .env; set +a

# Fresh setup only: pull the DB from the release if there's no local copy.
[ -f data/tyler.db ] || gh release download data-latest -p tyler.db -D data || true

# The pipeline: ingest -> rank -> harvest -> match -> prune (args pass through)
/usr/bin/python3 -m src.daily "$@" || { echo "pipeline FAILED"; exit 1; }

# Publish the updated DB as the rolling Release asset the dashboard downloads
gh release upload data-latest data/tyler.db --clobber || echo "release upload failed"

# Nudge Streamlit Cloud to redeploy with fresh data
date -u +"%Y-%m-%dT%H:%M:%SZ" > data/last_update.txt
git add data/last_update.txt
git commit -m "data: daily update $(date -u +%F)" || true
git push origin main || true

# Did ranking actually store predictions today? Only then mark the day done, so
# later fires stop retrying. A credits-out 402 leaves 0 predictions (the
# pipeline still exits 0 and pushes the harvest/match updates), so it correctly
# does NOT mark done and the next fire retries. --no-rank never marks done.
STORED="$(/usr/bin/python3 -c "import sqlite3; print(sqlite3.connect('data/tyler.db').execute(\"select count(*) from predictions where run_date=?\", ('$TODAY',)).fetchone()[0])" 2>/dev/null || echo 0)"
if [ "${STORED:-0}" -gt 0 ]; then
  echo "$TODAY" > "$MARKER"
  echo "[guard] success: $STORED prediction(s) stored for $TODAY — marking day done."
else
  echo "[guard] no predictions stored for $TODAY (offline / out of credits?) — a later fire will retry."
fi

echo "===== done $(date -u +%FT%TZ) ====="
