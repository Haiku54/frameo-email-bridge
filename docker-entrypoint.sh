#!/bin/bash
set -e

# If the caller passed arguments (e.g. `docker compose run --rm app python
# configure.py`), execute them directly. The skip-validation path lets us
# bootstrap config.yaml interactively from inside the container without
# tripping the "config missing" guard below.
if [ $# -gt 0 ]; then
    exec "$@"
fi

# Default path: run the bridge service. Validate that config.yaml has
# been provided via a bind mount. If the user ran `docker compose up`
# without first creating config.yaml on the host, Docker will have
# bind-mounted an empty *directory* at /app/config.yaml instead of a
# file — detect that and fail fast with a clear message.

if [ -d /app/config.yaml ]; then
    echo "ERROR: /app/config.yaml is a directory, not a file." >&2
    echo "" >&2
    echo "This usually means config.yaml did not exist on the host when" >&2
    echo "you ran 'docker compose up'. Docker then created an empty" >&2
    echo "directory where the bind mount points." >&2
    echo "" >&2
    echo "To fix it:" >&2
    echo "  1. Stop and remove the container:  docker compose down" >&2
    echo "  2. Remove the empty directory:     rmdir config.yaml credentials.json 2>/dev/null" >&2
    echo "  3. Generate a real config.yaml:    bash setup.sh" >&2
    echo "     (or run inside the container:   docker compose run --rm frameo-bridge python configure.py)" >&2
    echo "  4. Start the container again:      docker compose up -d" >&2
    exit 1
fi

if [ ! -f /app/config.yaml ]; then
    echo "ERROR: /app/config.yaml is missing." >&2
    echo "Mount it with: -v /path/to/config.yaml:/app/config.yaml" >&2
    exit 1
fi

# Warn if Google Photos sync is enabled but credentials.json is missing
# or got bind-mounted as an empty directory (host file did not exist).
# Non-fatal: main.py wraps GP setup in try/except, so the bridge will
# still service email→frame even if GP can't initialise.
if grep -q "enabled: true" /app/config.yaml 2>/dev/null; then
    if [ ! -f /app/credentials.json ]; then
        echo "WARNING: google_photos is enabled but credentials.json is not a regular file." >&2
        echo "Google Photos sync will be disabled. To fix:" >&2
        echo "  1. Place credentials.json in the project directory on the host" >&2
        echo "  2. Run: python google_photos.py --auth (on the host or inside the container)" >&2
        echo "  3. Restart the container" >&2
        echo "" >&2
    fi
fi

exec python main.py
