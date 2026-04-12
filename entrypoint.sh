#!/bin/bash
# Context Broker — Entrypoint script
# REQ-CB §1.5: Wire package source from config.yml at container startup.
#
# Reads packages.source from config.yml and installs dependencies from
# the appropriate source before starting the application server.

set -e

# ERQ-005 §2.4: umask 000 for world-writable file creation
umask 000

# Defense-in-depth: data dirs should exist from git clone (.gitkeep),
# but guard against edge cases where bind mount has wrong permissions.
mkdir -p /data/downloads 2>/dev/null || true

CONFIG_FILE="${CONFIG_PATH:-/config/config.yml}"

if [ -f "$CONFIG_FILE" ]; then
    # Extract package source from config.yml using Python (available in the image)
    PKG_SOURCE=$(python3 -c "
import yaml, sys
try:
    with open('$CONFIG_FILE') as f:
        cfg = yaml.safe_load(f)
    pkgs = cfg.get('packages', {})
    print(pkgs.get('source', 'pypi'))
except Exception:
    print('pypi')
" 2>/dev/null || echo "pypi")

    PKG_LOCAL_PATH=$(python3 -c "
import yaml, sys
try:
    with open('$CONFIG_FILE') as f:
        cfg = yaml.safe_load(f)
    pkgs = cfg.get('packages', {})
    print(pkgs.get('local_path', '/app/packages'))
except Exception:
    print('/app/packages')
" 2>/dev/null || echo "/app/packages")

    PKG_DEVPI_URL=$(python3 -c "
import yaml, sys
try:
    with open('$CONFIG_FILE') as f:
        cfg = yaml.safe_load(f)
    pkgs = cfg.get('packages', {})
    url = pkgs.get('devpi_url')
    print(url if url else '')
except Exception:
    print('')
" 2>/dev/null || echo "")

    echo "Package source: $PKG_SOURCE"

    case "$PKG_SOURCE" in
        local)
            # Requirements are already installed at build time from PyPI.
            # Only StateGraph packages come from the local path (handled below).
            # RB-34: packages mount is :ro — source copied to /tmp for install.
            echo "Package source is local — using build-time requirements"
            ;;
        devpi)
            if [ -n "$PKG_DEVPI_URL" ]; then
                echo "Installing packages from devpi: $PKG_DEVPI_URL"
                pip install --user --no-cache-dir --index-url "$PKG_DEVPI_URL" -r /app/requirements.txt
            else
                echo "devpi_url not set, skipping package install"
            fi
            ;;
        pypi)
            # Packages already installed at build time; skip unless requirements changed
            echo "Package source is pypi — using build-time packages"
            ;;
        *)
            echo "Unknown package source: $PKG_SOURCE — using build-time packages"
            ;;
    esac
else
    echo "Config file not found at $CONFIG_FILE — using build-time packages"
fi


# ── Install StateGraph packages (AE + TE) from pre-built wheels ──────────────
# Wheels were built at image build time (pip wheel --no-deps) and stored in
# /app/stategraph-wheels/. Installing with --no-deps ensures no transitive
# dependency upgrades occur — only the AE/TE code itself is installed.
# Use install_stategraph to update AE/TE in a running container.
echo "Installing StateGraph packages from pre-built wheels (--no-deps)"
for wheel in /app/stategraph-wheels/*.whl; do
    if [ -f "$wheel" ]; then
        echo "Installing wheel: $wheel"
        pip install --user --no-deps --no-cache-dir "$wheel"
    fi
done

# Start the application
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
