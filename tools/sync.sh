#!/usr/bin/env bash
# Optional helper (the only shell file here): copy this project from the authoring machine (e.g. Windows Git Bash) to a
# jump box. Settings from the environment or .sync.env (gitignored):
#   SYNC_HOST=user@jumpbox   SYNC_DIR=ocp-px-csi-iscsi2nvmetcp (relative to the remote home)   SYNC_SSH_OPTS="-i ~/.ssh/key"
# Never copied: .state/ (kubeconfig, array token, logs), .git, .sync.env. tar never deletes: remove renamed or deleted
# files on the jump box yourself.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
[[ -f $ROOT/.sync.env ]] && source "$ROOT/.sync.env"
: "${SYNC_HOST:?set SYNC_HOST=user@host (env or .sync.env)}"
SYNC_DIR=${SYNC_DIR:-ocp-px-csi-iscsi2nvmetcp}
# shellcheck disable=SC2086
tar -C "$ROOT" -czf - --exclude=./.git --exclude=./.state --exclude=./.sync.env --exclude=__pycache__ . \
  | ssh ${SYNC_SSH_OPTS:-} "$SYNC_HOST" "mkdir -p '$SYNC_DIR' && tar -xzf - -C '$SYNC_DIR'"
echo "synced to $SYNC_HOST:$SYNC_DIR"
