#!/bin/sh
# `serve` (default) listens on all interfaces inside the container; anything else is passed to the digest CLI.
set -eu

CONFIG="${DIGEST_CONFIG:-/config/config.toml}"
if [ ! -f "$CONFIG" ]; then
  echo "Config non trovata: $CONFIG" >&2
  echo "Copia docker/config.docker.toml in ./config/config.toml, modificala e riavvia (vedi README, sezione Docker)." >&2
  exit 2
fi

command="${1:-serve}"
if [ "$#" -gt 0 ]; then shift; fi

if [ "$command" = "serve" ]; then
  exec python -m digest -c "$CONFIG" serve --host 0.0.0.0 "$@"
fi
exec python -m digest -c "$CONFIG" "$command" "$@"
