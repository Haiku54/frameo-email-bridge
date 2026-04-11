#!/bin/bash
set -e

# Validate that config.yaml has been provided via a bind mount.
# If the user ran `docker compose up` without first creating config.yaml
# on the host, Docker will have bind-mounted an empty *directory* at
# /app/config.yaml instead of a file — detect that and fail fast with
# a clear message.

if [ -d /app/config.yaml ]; then
    echo "ERROR: /app/config.yaml is a directory, not a file." >&2
    echo "" >&2
    echo "This usually means config.yaml did not exist on the host when" >&2
    echo "you ran 'docker compose up'. Docker then created an empty" >&2
    echo "directory where the bind mount points." >&2
    echo "" >&2
    echo "To fix it:" >&2
    echo "  1. Stop and remove the container:  docker compose down" >&2
    echo "  2. Remove the empty directory:     rmdir config.yaml" >&2
    echo "  3. Generate a real config.yaml:    bash setup.sh" >&2
    echo "     (or copy config.yaml.example and edit it manually)" >&2
    echo "  4. Start the container again:      docker compose up -d" >&2
    exit 1
fi

if [ ! -f /app/config.yaml ]; then
    echo "ERROR: /app/config.yaml is missing." >&2
    echo "Mount it with: -v /path/to/config.yaml:/app/config.yaml:ro" >&2
    exit 1
fi

exec python main.py
