#!/bin/sh
set -eu
INITIAL_PASSWORD=$(cat /run/secrets/omniroute_password)
export INITIAL_PASSWORD
exec node /opt/omniroute/node_modules/omniroute/bin/omniroute.mjs serve --no-open --no-tray --port 20128
