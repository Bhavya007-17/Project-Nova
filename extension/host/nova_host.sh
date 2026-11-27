#!/bin/sh
# Nova's native-messaging host for Chrome on macOS (SPEC 9). Chrome starts this; it relays to Nova's socket.
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/../../.venv/bin/python" -I -u "$DIR/nova_host.py" "$@"
