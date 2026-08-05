FROM python:3.12-slim-bookworm
LABEL maintainer="Rob Smith https://github.com/RobXYZ"

# TARGETARCH is set automatically by `docker buildx build` (amd64,
# arm64, …). Plain `docker build` does NOT set it; fall back to dpkg.
ARG TARGETARCH

# System deps:
# - python is in the base image; pip installs web deps (PEP 668 ->
#   --break-system-packages, safe in a container).
# - jellyfin-ffmpeg7: exports + thumbnails. Unlike Debian's stock
#   ffmpeg (and unlike Alpine's musl build), the Jellyfin bundle ships
#   the *legacy* Intel Media SDK runtime alongside oneVPL, which is what
#   the DS920+'s Gen-9.5 (Gemini Lake) iGPU needs for QuickSync. Stock
#   runtimes only drive Gen 12+, failing Gen 9.5 with "MFX session: -9";
#   the bundle is exactly how Jellyfin solved QSV-in-Docker. It also
#   bundles the iHD VAAPI driver, so VAAPI keeps working as a fallback.
#   The binary installs to /usr/lib/jellyfin-ffmpeg/{ffmpeg,ffprobe};
#   we symlink it onto PATH so shutil.which("ffmpeg") finds it unchanged.
# - gosu: privilege drop in entrypoint.sh (Debian's su-exec equivalent;
#   same initgroups() semantics so the GPU render-group logic in
#   setuid.sh keeps working).
# - vainfo (amd64): a one-line passthrough diagnostic. (On Debian the
#   binary ships in the `vainfo` package, not Alpine's `libva-utils`.)
# On arm64 jellyfin-ffmpeg installs too but QSV simply won't probe-pass;
# exports degrade to software/VAAPI exactly as before. The app's encoder
# probe (web/services/exporter.py) runtime-tests every candidate and
# falls back to libx264, so a host without a working iGPU degrades
# transparently.
RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash ca-certificates gnupg gosu tzdata; \
    install -d /etc/apt/keyrings; \
    gpg_url="https://repo.jellyfin.org/jellyfin_team.gpg.key"; \
    apt-get install -y --no-install-recommends curl; \
    curl -fsSL "$gpg_url" | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg] https://repo.jellyfin.org/debian bookworm main" \
        > /etc/apt/sources.list.d/jellyfin.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends jellyfin-ffmpeg7; \
    case "$arch" in \
        amd64) apt-get install -y --no-install-recommends vainfo ;; \
    esac; \
    ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg; \
    ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe; \
    apt-get purge -y curl gnupg; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    useradd -UMr dashcam

COPY LICENSE /
COPY setuid.sh /setuid.sh
COPY entrypoint.sh /entrypoint.sh

ENV PUID="" \
    PGID="" \
    RECORDINGS="/recordings"

# Install Python deps into the system site-packages. pip refuses
# by default (PEP 668 on Debian Bookworm+); --break-system-packages
# is safe inside a container.
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir --break-system-packages \
        -r /requirements.txt

# Version stamp, injected by CI (metadata-action version + git sha).
# Declared after the dependency layers so changing it never busts the
# apt/pip cache. Local builds without args report "dev" in the UI.
ARG VERSION=dev
ARG REVISION=""
ENV VIOFOSYNC_VERSION="$VERSION" \
    VIOFOSYNC_REVISION="$REVISION"

COPY --chown=dashcam viofosync_lib /viofosync_lib
COPY --chown=dashcam web /web

EXPOSE 8080

RUN sed -i 's/\r$//' /entrypoint.sh /setuid.sh \
    && chmod +x /entrypoint.sh /setuid.sh

ENTRYPOINT [ "/entrypoint.sh"]
