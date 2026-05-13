#!/bin/sh
# Write pgbouncer.ini and userlist.txt from environment variables at container startup.

: "${DB_HOST:?DB_HOST is required}"
: "${DB_PORT:=5432}"
: "${DB_NAME:?DB_NAME is required}"
: "${DB_USER:?DB_USER is required}"
: "${DB_PASSWORD:?DB_PASSWORD is required}"
: "${POOL_MODE:=transaction}"
: "${MAX_CLIENT_CONN:=200}"
: "${DEFAULT_POOL_SIZE:=25}"
: "${SERVER_TLS_SSLMODE:=verify-ca}"

mkdir -p /etc/pgbouncer

# Resolve DB_HOST to its IPv4 address so PgBouncer never attempts IPv6.
# On Windows/WSL2, host.docker.internal can resolve to an IPv6 address that
# is unreachable inside Docker, causing server_login_retry backoff errors.
_resolved=$(getent ahosts "${DB_HOST}" 2>/dev/null | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/ { print $1; exit }')
[ -n "$_resolved" ] && DB_HOST="$_resolved"

cat > /etc/pgbouncer/pgbouncer.ini <<EOF
[databases]
${DB_NAME} = host=${DB_HOST} port=${DB_PORT} dbname=${DB_NAME}

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 5432
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = ${POOL_MODE}
max_client_conn = ${MAX_CLIENT_CONN}
default_pool_size = ${DEFAULT_POOL_SIZE}
server_tls_sslmode = ${SERVER_TLS_SSLMODE}
server_tls_ca_file = /etc/pgbouncer/ca-certificate.crt
server_reset_query = DISCARD ALL
log_connections = 0
log_disconnections = 0
log_pooler_errors = 1
EOF

# Keep the plain password here so PgBouncer can authenticate to Postgres
# when the server requires SCRAM authentication.
echo "\"${DB_USER}\" \"${DB_PASSWORD}\"" > /etc/pgbouncer/userlist.txt

exec "$@"
