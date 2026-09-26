#!/usr/bin/env bash
# Create dedicated stream.domino101.com site (requires MariaDB root).
# Until then, domino_stream can run on www.domino101.com.
set -euo pipefail
cd "$(dirname "$0")/../../.."
SITE="${1:-stream.domino101.com}"
echo "Creating site $SITE with domino_stream..."
bench new-site "$SITE" --install-app domino_stream "$@"
echo "Next: configure Stream Settings + DNS/TLS; set www Domino Defaults Domino Stream Site URL."
