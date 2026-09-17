#!/usr/bin/env bash
# Resolve the base image tag to a digest and write it into the Dockerfile.
#
# Run this deliberately — when you *intend* to move to a newer base — rather
# than on every build. That is the whole point of pinning.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-python:3.12-slim-bookworm}"
echo "==> pulling $TAG"
docker pull "$TAG" >/dev/null
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' "$TAG" | cut -d@ -f2)
[ -n "$DIGEST" ] || { echo "could not resolve a digest for $TAG" >&2; exit 1; }

echo "==> $TAG is $DIGEST"
sed -i.bak -E "s|^FROM ${TAG%%:*}:[^@]+(@sha256:[a-f0-9]+)?|FROM ${TAG}@${DIGEST}|" Dockerfile
rm -f Dockerfile.bak
grep '^FROM' Dockerfile
echo "==> Dockerfile updated. Commit it, then rebuild:  docker compose build"
