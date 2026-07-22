#!/usr/bin/env sh
# Build and deploy NewsPrism, with a version tag from Git.
#
# Burns VERSION (release tag or Git hash) and BUILD_DATE into the image so the
# dashboard shows the version. Releases are made via tags
# (e.g. in Forgejo: git tag -a v1.0 -m "..."); without a tag,
# git describe yields the short hash.
#
# A freshly started container does NOT run immediately by default
# (schedule.run_on_start: false) - it waits one interval and then follows the
# schedule. So the default here is just build + restart, and the next run
# happens on schedule. To trigger a run right now, use --force-run (SIGUSR1).
# (Set schedule.run_on_start: true in config.yaml to make every start run.)
#
# Usage:
#   ./build.sh              build + restart (next run on schedule, not immediately)
#   ./build.sh --build      build only
#   ./build.sh --force-run  force a running container into an immediate run via SIGUSR1
#                           (no build, no restart)
set -eu

SERVICE=newsprism

case "${1:-}" in
    --force-run)
        docker kill -s SIGUSR1 "${SERVICE}"
        echo "[build.sh] Triggered an immediate run in the running container (SIGUSR1)."
        exit 0
        ;;
esac

# Version from Git: tag if present, otherwise short hash; -dirty on uncommitted
# changes. Falls back to "dev" if no Git is available.
if VERSION="$(git describe --tags --always --dirty 2>/dev/null)"; then
    :
else
    VERSION=dev
fi
BUILD_DATE="$(date +%Y-%m-%d)"

echo "[build.sh] Version: ${VERSION}  Date: ${BUILD_DATE}"

docker compose build "${SERVICE}" \
    --build-arg VERSION="${VERSION}" \
    --build-arg BUILD_DATE="${BUILD_DATE}"

case "${1:-}" in
    --build)
        echo "[build.sh] built only."
        ;;
    *)
        docker compose up -d "${SERVICE}"
        echo "[build.sh] built + restarted (next run on schedule; --force-run to run now)."
        ;;
esac
