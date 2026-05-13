FROM edoburu/pgbouncer:v1.23.1-p2

USER root

# Copy config templates — actual values come from env vars at runtime via entrypoint
COPY entrypoint.sh /entrypoint.sh

RUN set -eux; \
    apk add --no-cache netcat-openbsd; \
    if ! id -u pgbouncer >/dev/null 2>&1; then \
      addgroup -S pgbouncer; \
      adduser -S -G pgbouncer pgbouncer; \
    fi; \
    mkdir -p /etc/pgbouncer; \
    chown -R pgbouncer:pgbouncer /etc/pgbouncer; \
    chmod +x /entrypoint.sh

USER pgbouncer

EXPOSE 5432

ENTRYPOINT ["/entrypoint.sh"]
CMD ["pgbouncer", "/etc/pgbouncer/pgbouncer.ini"]
