#!/usr/bin/env bash
# Cloud runner for the hourly claude.ai routine. The container is wiped between
# runs, so state.json and log.jsonl live on the orphan branch `watch-state`.
#
#   ./cloud_run.sh check     restore state, poll once, print outbox.jsonl
#   ./cloud_run.sh persist   commit state.json + log.jsonl to watch-state, push
#
# Run `persist` only AFTER the e-mails from the outbox went out: if sending
# fails, the next run sees the same news again instead of losing it.
set -uo pipefail
cd "$(dirname "$0")"
BRANCH=watch-state

case "${1:-}" in
  check)
    cp config.cloud.json config.json
    rm -f outbox.jsonl
    if git fetch -q origin "$BRANCH" 2>/dev/null; then
      git show "origin/$BRANCH:state.json" > state.json 2>/dev/null || rm -f state.json
      git show "origin/$BRANCH:log.jsonl" > log.jsonl 2>/dev/null || rm -f log.jsonl
    else
      echo "NOTE: no $BRANCH branch yet - cold start, seeding silently."
    fi
    python3 prag_watch.py check
    status=$?
    echo "--- OUTBOX ---"
    if [ -s outbox.jsonl ]; then cat outbox.jsonl; else echo "(leer - nichts zu melden)"; fi
    exit $status
    ;;
  persist)
    [ -f state.json ] || { echo "no state.json - run check first"; exit 1; }
    idx=$(mktemp)
    export GIT_INDEX_FILE=$idx
    git read-tree --empty
    for f in state.json log.jsonl; do
      [ -f "$f" ] && git update-index --add --cacheinfo "100644,$(git hash-object -w "$f"),$f"
    done
    tree=$(git write-tree)
    unset GIT_INDEX_FILE; rm -f "$idx"
    msg="watch state $(date -u +%Y-%m-%dT%H:%MZ)"
    if parent=$(git rev-parse -q --verify "origin/$BRANCH"); then
      commit=$(git -c user.name=prag-watch -c user.email=prag-watch@users.noreply.github.com \
               commit-tree "$tree" -p "$parent" -m "$msg")
    else
      commit=$(git -c user.name=prag-watch -c user.email=prag-watch@users.noreply.github.com \
               commit-tree "$tree" -m "$msg")
    fi
    for i in 1 2 3 4; do
      git push -q origin "$commit:refs/heads/$BRANCH" && { echo "state pushed ($commit)"; exit 0; }
      sleep $((2 ** i))
    done
    echo "ERROR: push to $BRANCH failed"; exit 1
    ;;
  *)
    sed -n 2,10p "$0"; exit 2 ;;
esac
