#!/bin/sh
# Copy the shared README and LICENSE into both packages before publishing.
cd "$(dirname "$0")" && for d in npm python; do cp README.md LICENSE "$d/"; done
