FROM alpine:3.20.3

RUN apk add --no-cache bash python3 shadow tzdata \
    && useradd -UMr dashcam

COPY COPYING /
COPY setuid.sh /setuid.sh
COPY entrypoint.sh /entrypoint.sh
COPY --chown=dashcam viofosync.py /viofosync.py

RUN sed -i 's/\r$//' /entrypoint.sh /setuid.sh /viofosync.py \
    && chmod +x /entrypoint.sh /setuid.sh

ENV ADDRESS="" \
    PUID="" \
    PGID="" \
    KEEP="" \
    GROUPING="" \
    PRIORITY="" \
    MAX_USED_DISK="" \
    TIMEOUT="" \
    VERBOSE=0 \
    QUIET="" \
    DRY_RUN="" \
    RUN_ONCE="" \
    GPS_EXTRACT=""

ENTRYPOINT ["/entrypoint.sh"]
