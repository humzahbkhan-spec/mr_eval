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

# Wait for real network before doing anything. launchd fires this at 9am even
# if the Mac only just woke and wifi hasn't associated yet — a run started then
# gets ~90% DNS failures (Errno 8), ranks a crippled pool, and fails to push.
# Poll up to ~10 min; if still offline, skip cleanly rather than poison the data
# or burn API budget. A skipped day is an honest gap; a degraded day is not.
online() { /usr/bin/python3 -c "import socket; socket.setdefaulttimeout(5); socket.gethostbyname('marginalrevolution.com')" 2>/dev/null; }
for i in $(seq 1 60); do
  online && break
  echo "[wait] no network yet (try $i/60), sleeping 10s…"
  sleep 10
done
if ! online; then
  echo "[wait] still offline after ~10 min — skipping this run (no degraded data)."
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

echo "===== done $(date -u +%FT%TZ) ====="
