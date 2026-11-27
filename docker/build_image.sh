#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"

# Build the shared test image explicitly. Test runs reuse the same tag.
docker build -t localization-test:latest .
