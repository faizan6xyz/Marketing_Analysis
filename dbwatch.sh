#!/bin/bash
# PostgreSQL HA controller: db <-> db2 (podman)
#
# Required env : DB_PASSWORD
# Optional env : DB_USER DB_NAME PRIMARY_DB CHECK_INTERVAL FAIL_THRESHOLD
#                AUTO_PROMOTE AUTO_FENCE AUTO_DEMOTE AUTO_FAILBACK FAILBACK_CMD
#                DB1_CONTAINER DB2_CONTAINER DB1_VOLUME DB2_VOLUME
#                PODMAN_NETWORK PG_IMAGE
set -u

export PGPASSWORD="${DB_PASSWORD:?DB_PASSWORD is not set}"
export PGCONNECT_TIMEOUT=3

# ---------------------------------------------------------------
# Config
# ---------------------------------------------------------------
DB1_HOST="${DB1_HOST:-db}"
DB2_HOST="${DB2_HOST:-db2}"
DB1_CONTAINER="${DB1_CONTAINER:-$DB1_HOST}"
DB2_CONTAINER="${DB2_CONTAINER:-$DB2_HOST}"
DB1_VOLUME="${DB1_VOLUME:-myapp_db_data}"
DB2_VOLUME="${DB2_VOLUME:-myapp_db2_data}"

# Network the one-off pg_rewind container joins, so it can resolve db / db2.
PODMAN_NETWORK="${PODMAN_NETWORK:-myapp_default}"
PG_IMAGE="${PG_IMAGE:-docker.io/library/postgres:16}"

DB_USER="${DB_USER:-leojr}"
DB_NAME="${DB_NAME:-userdb}"

# Last known good primary. Seed it (PRIMARY_DB=db or db2) if the controller
# might start while both nodes are already primary.
PRIMARY_DB="${PRIMARY_DB:-}"

INTERVAL="${CHECK_INTERVAL:-3}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-5}"
AUTO_PROMOTE="${AUTO_PROMOTE:-false}"
AUTO_FENCE="${AUTO_FENCE:-true}"
AUTO_DEMOTE="${AUTO_DEMOTE:-true}"
AUTO_FAILBACK="${AUTO_FAILBACK:-false}"
FAILBACK_CMD="${FAILBACK_CMD:-}"

# ---------------------------------------------------------------
# Single instance lock
# ---------------------------------------------------------------
exec 9>/tmp/ha-controller.lock
if ! flock -n 9; then
    echo "$(date '+%F %T') CRIT: another ha-controller instance is already running"
    exit 1
fi

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
ts()  { date '+%F %T'; }
log() { echo "$(ts) $*"; }

trap 'log "INFO: controller stopped"; exit 0' INT TERM

q() {
    psql -X -h "$1" -U "$DB_USER" -d "$DB_NAME" -Atc "$2" 2>/dev/null
}

role() {
    local host="$1"
    local r

    if ! pg_isready -q -h "$host" -U "$DB_USER" -d "$DB_NAME"; then
        echo "down"
        return
    fi

    r=$(q "$host" "SELECT pg_is_in_recovery();")
    case "$r" in
        f) echo "primary" ;;
        t) echo "standby" ;;
        *) echo "unknown" ;;
    esac
}

repl_info() {
    local row
    row=$(q "$1" "
        SELECT
            count(*),
            COALESCE(max(pg_wal_lsn_diff(sent_lsn, replay_lsn)), 0)::bigint
        FROM pg_stat_replication;
    ")
    echo "${row:-0|0}"
}

# true if $1 has a WAL receiver in 'streaming' state
is_streaming() {
    local n
    n=$(q "$1" "SELECT count(*) FROM pg_stat_wal_receiver WHERE status = 'streaming';")
    [ "${n:-0}" -ge 1 ]
}

# ---------------------------------------------------------------
# Promote a standby
# ---------------------------------------------------------------
promote_node() {
    local host="$1"
    local result i

    log "PROMOTE: requesting promotion of $host..."

    result=$(psql -X -h "$host" -U "$DB_USER" -d "$DB_NAME" \
        -Atc "SELECT pg_promote();" 2>&1)

    if [ "$result" != "t" ]; then
        log "CRIT: pg_promote() on $host did not succeed"
        log "DETAIL: $result"
        return 1
    fi

    log "PROMOTE: pg_promote() accepted for $host"

    for i in {1..10}; do
        sleep 1
        if [ "$(role "$host")" = "primary" ]; then
            log "PROMOTE: $host is now PRIMARY"
            return 0
        fi
        log "PROMOTE: waiting for $host... ($i/10)"
    done

    log "CRIT: $host did not become PRIMARY"
    return 1
}

# ---------------------------------------------------------------
# Fence a node (best effort; the real fence is stopping the container)
# default_transaction_read_only can be overridden per session, so it only
# narrows the window until demote_to_standby stops the container.
# pg_rewind later overwrites postgresql.auto.conf, so it does not persist.
# ---------------------------------------------------------------
fence_node() {
    local host="$1"
    log "FENCE: setting $host to read-only (best effort)"
    q "$host" "ALTER SYSTEM SET default_transaction_read_only = on;" >/dev/null
    q "$host" "SELECT pg_reload_conf();" >/dev/null
}

# ---------------------------------------------------------------
# Demote a primary to a standby of another primary
#   demote_to_standby <loser_container> <loser_host> <loser_volume> <winner_host>
# Any writes that only exist on the loser are discarded by pg_rewind.
# ---------------------------------------------------------------
demote_to_standby() {
    local l_container="$1" l_host="$2" l_volume="$3" w_host="$4"
    local i
    local net_args=()

    log "DEMOTE: turning $l_host into a STANDBY of $w_host..."

    # 0. The winner must really be primary
    if [ "$(role "$w_host")" != "primary" ]; then
        log "CRIT: $w_host is not primary, refusing to demote $l_host"
        return 1
    fi

    # pg_rewind requires the source to have checkpointed after its promotion
    q "$w_host" "CHECKPOINT;" >/dev/null

    # 1. Stop the loser (postgres is PID 1, the image's stop signal = fast shutdown)
    log "DEMOTE: stopping $l_container..."
    if ! podman stop -t 60 "$l_container" >/dev/null; then
        log "CRIT: failed to stop $l_container"
        return 1
    fi

    # 2. Rewind + write standby.signal and primary_conninfo
    if [ -n "$PODMAN_NETWORK" ]; then
        net_args=(--network "$PODMAN_NETWORK")
    fi

    log "DEMOTE: rewinding $l_host to follow $w_host..."
    if ! podman run --rm --user postgres \
            ${net_args[@]+"${net_args[@]}"} \
            -v "$l_volume":/var/lib/postgresql/data \
            "$PG_IMAGE" \
            pg_rewind \
              --target-pgdata=/var/lib/postgresql/data \
              --source-server="host=$w_host port=5432 user=$DB_USER password=$DB_PASSWORD dbname=$DB_NAME" \
              --write-recovery-conf --progress; then
        log "CRIT: pg_rewind failed, $l_container is left STOPPED"
        log "CRIT: rebuild it by hand, e.g. wipe volume $l_volume and run pg_basebackup -R from $w_host"
        return 1
    fi

    # 3. Start it (standby.signal makes it come up as a standby)
    log "DEMOTE: starting $l_container as standby..."
    if ! podman start "$l_container" >/dev/null; then
        log "CRIT: failed to start $l_container"
        return 1
    fi

    # 4. Wait for the standby role
    for i in {1..30}; do
        sleep 1
        if [ "$(role "$l_host")" = "standby" ]; then
            log "DEMOTE: $l_host is now STANDBY"
            break
        fi
        log "DEMOTE: waiting for $l_host... ($i/30)"
        if [ "$i" -eq 30 ]; then
            log "CRIT: $l_host did not become STANDBY"
            return 1
        fi
    done

    # 5. Confirm it is actually streaming from the winner
    for i in {1..15}; do
        if is_streaming "$l_host"; then
            log "DEMOTE: $l_host is streaming from $w_host"
            return 0
        fi
        sleep 1
    done

    log "CRIT: $l_host is STANDBY but not streaming from $w_host"
    log "CRIT: check primary_conninfo, pg_hba.conf and the REPLICATION privilege of $DB_USER"
    return 1
}

# ---------------------------------------------------------------
# Split-brain: both nodes primary
# ---------------------------------------------------------------
handle_split_brain() {
    local w_host l_host l_container l_volume

    log "CRIT: SPLIT-BRAIN - $DB1_HOST AND $DB2_HOST are both PRIMARY"
    log "CRIT: DO NOT write to both databases."

    case "$PRIMARY_DB" in
        "$DB1_HOST")
            w_host="$DB1_HOST"
            l_host="$DB2_HOST"; l_container="$DB2_CONTAINER"; l_volume="$DB2_VOLUME"
            ;;
        "$DB2_HOST")
            w_host="$DB2_HOST"
            l_host="$DB1_HOST"; l_container="$DB1_CONTAINER"; l_volume="$DB1_VOLUME"
            ;;
        *)
            log "CRIT: last known primary is unknown - refusing to guess which node to demote"
            log "CRIT: restart with PRIMARY_DB=$DB1_HOST or PRIMARY_DB=$DB2_HOST, or demote manually"
            return 1
            ;;
    esac

    log "CRIT: keeping $w_host as PRIMARY, demoting $l_host (its unreplicated writes will be lost)"

    if [ "$AUTO_FENCE" = "true" ]; then
        fence_node "$l_host"
    fi

    if [ "$AUTO_DEMOTE" != "true" ]; then
        log "WARN: AUTO_DEMOTE=false, $l_host left running - demote it manually"
        return 1
    fi

    demote_to_standby "$l_container" "$l_host" "$l_volume" "$w_host"
}

# ---------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------
FAILS=0

log "============================================================"
log "PostgreSQL HA controller started"
log "db  = $DB1_HOST (container $DB1_CONTAINER, volume $DB1_VOLUME)"
log "db2 = $DB2_HOST (container $DB2_CONTAINER, volume $DB2_VOLUME)"
log "known primary = ${PRIMARY_DB:-<unknown>}"
log "interval      = ${INTERVAL}s"
log "threshold     = ${FAIL_THRESHOLD}"
log "auto promote  = ${AUTO_PROMOTE}"
log "auto fence    = ${AUTO_FENCE}"
log "auto demote   = ${AUTO_DEMOTE}"
log "auto failback = ${AUTO_FAILBACK}"
log "============================================================"

while true; do
    R1=$(role "$DB1_HOST")
    R2=$(role "$DB2_HOST")

    # ---------------- split-brain ----------------
    if [ "$R1" = "primary" ] && [ "$R2" = "primary" ]; then
        handle_split_brain

    # ---------------- db is primary ----------------
    elif [ "$R1" = "primary" ]; then
        PRIMARY_DB="$DB1_HOST"
        FAILS=0

        ROW=$(repl_info "$DB1_HOST")
        STANDBYS=${ROW%%|*}
        LAG=${ROW##*|}

        if [ "${STANDBYS:-0}" -eq 0 ]; then
            log "WARN: db is PRIMARY but db2 is NOT replicating (db2=$R2)"
        else
            log "OK: db PRIMARY, db2 streaming, lag=${LAG} bytes"
        fi

    # ---------------- db is down ----------------
    elif [ "$R1" = "down" ]; then
        FAILS=$((FAILS + 1))
        log "FAIL: db unreachable ($FAILS/$FAIL_THRESHOLD)"

        case "$R2" in
            standby)
                log "INFO: db2 is reachable as STANDBY"

                if [ "$FAILS" -ge "$FAIL_THRESHOLD" ]; then
                    if [ "$AUTO_PROMOTE" != "true" ]; then
                        log "WARN: AUTO_PROMOTE=false, db2 remains STANDBY"
                    elif is_streaming "$DB2_HOST"; then
                        log "WARN: db2 is still streaming from db - db looks ALIVE"
                        log "WARN: refusing to promote (would cause split-brain)"
                    else
                        log "FAILOVER: db failed, db2 stopped receiving WAL"
                        log "FAILOVER: promoting db2..."

                        if promote_node "$DB2_HOST"; then
                            PRIMARY_DB="$DB2_HOST"
                            log "================================================"
                            log "FAILOVER COMPLETE"
                            log "db2 = PRIMARY"
                            log "db  = FAILED"
                            log "Repoint apps to db2."
                            log "================================================"
                            FAILS=0
                        else
                            log "CRIT: FAILOVER FAILED"
                        fi
                    fi
                fi
                ;;
            primary)
                PRIMARY_DB="$DB2_HOST"
                log "OK: db2 is already PRIMARY (serving traffic)"
                log "INFO: db is currently unavailable"
                FAILS=0
                ;;
            down)
                log "CRIT: BOTH db AND db2 are unreachable"
                ;;
            *)
                log "CRIT: db2 reachable but role is unknown"
                ;;
        esac

    # ---------------- db2 is primary, db is standby ----------------
    elif [ "$R1" = "standby" ] && [ "$R2" = "primary" ]; then
        PRIMARY_DB="$DB2_HOST"
        FAILS=0

        ROW=$(repl_info "$DB2_HOST")
        STANDBYS=${ROW%%|*}
        LAG=${ROW##*|}

        log "INFO: db2 is PRIMARY, db is STANDBY (streaming=${STANDBYS:-0}, lag=${LAG:-?} bytes)"

        if [ "$AUTO_FAILBACK" = "true" ] \
            && [ "${STANDBYS:-0}" -ge 1 ] \
            && [ "${LAG:-1}" -eq 0 ] \
            && [ -n "$FAILBACK_CMD" ]; then
            log "FAILBACK: db is caught up, running FAILBACK_CMD"
            if $FAILBACK_CMD; then
                log "FAILBACK: done. db should be PRIMARY, rebuild db2 as standby."
            else
                log "CRIT: FAILBACK_CMD failed - db2 remains PRIMARY"
            fi
        fi

    elif [ "$R1" = "unknown" ]; then
        log "WARN: db is reachable but its role is unknown (check credentials/connections)"

    else
        log "WARN: unexpected state db=$R1 db2=$R2 - needs investigation"
    fi

    sleep "$INTERVAL"
done