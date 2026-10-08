#!/bin/bash
set -u
export PGPASSWORD="${DB_PASSWORD:?DB_PASSWORD is not set}"
export PGCONNECT_TIMEOUT=3
DB1_HOST="db"
DB2_HOST="db2"
PRIMARY_DB=""
DB_USER="${DB_USER:-leojr}"
DB_NAME="${DB_NAME:-userdb}"
INTERVAL="${CHECK_INTERVAL:-3}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-5}"
AUTO_PROMOTE="${AUTO_PROMOTE:-false}"
AUTO_FENCE="${AUTO_FENCE:-true}"
AUTO_FAILBACK="${AUTO_FAILBACK:-false}"
FAILBACK_CMD="${FAILBACK_CMD:-}"
exec 9>/tmp/ha-controller.lock
if ! flock -n 9; then
    echo "$(date '+%F %T') CRIT: another ha-controller instance is already running"
    exit 1
fi
ts() {
    date '+%F %T'
}
log() {
    echo "$(ts) $*"
}
q() {
    psql -h "$1" -U "$DB_USER" -d "$DB_NAME" -Atc "$2" 2>/dev/null
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
db2_still_streaming_from_db() {
    local n
    n=$(q "$DB2_HOST" "SELECT count(*) FROM pg_stat_wal_receiver WHERE status = 'streaming';")
    [ "${n:-0}" -ge 1 ]
}
promote_db2() {
    local result
    local i
   log "PROMOTE: requesting db2 promotion..."
   result=$(psql \
        -h "$DB2_HOST" \
        -U "$DB_USER" \
        -d "$DB_NAME" \
        -Atc "SELECT pg_promote();" \
        2>&1)
   if [ "$result" != "t" ]; then
        log "CRIT: pg_promote() did not succeed"
        log "DETAIL: $result"
        return 1
    fi
   log "PROMOTE: pg_promote() accepted"
   for i in {1..10}; do
        sleep 1
       if [ "$(role "$DB2_HOST")" = "primary" ]; then
            log "PROMOTE: db2 is now PRIMARY"
            return 0
        fi
       log "PROMOTE: waiting for db2... ($i/10)"
    done
   log "CRIT: db2 did not become PRIMARY"
    return 1
}

promote_db() {
    local result
    local i
    log "PROMOTE: requesting db1 promotion..."
    result=$(psql \
        -h "$DB1_HOST" \
        -U "$DB_USER" \
        -d "$DB_NAME" \
        -Atc "SELECT pg_promote();" \
        2>&1)
    if [ "$result" != "t" ]; then
        log "CRIT: pg_promote() on db1 did not succeed"
        log "DETAIL: $result"
        return 1
    fi
    log "PROMOTE: pg_promote() accepted for db1"
    for i in {1..10}; do
        sleep 1
        if [ "$(role "$DB1_HOST")" = "primary" ]; then
            log "PROMOTE: db1 is now PRIMARY"
            return 0
        fi
        log "PROMOTE: waiting for db1... ($i/10)"
    done
    log "CRIT: db1 did not become PRIMARY"
    return 1
}
demote_db2_to_standby() {
    local i
    log "DEMOTE: preparing db2 to become STANDBY of db..."
 #    # 1. Stop writes on DB2
 #    log "DEMOTE: making db2 read-only..."
    if ! q "$DB2_HOST" \
        "ALTER SYSTEM SET default_transaction_read_only = on;" >/dev/null; then
        log "CRIT: failed to make db2 read-only"
        return 1
    fi
    q "$DB2_HOST" "SELECT pg_reload_conf();" >/dev/null
    # Verify
    if [ "$(q "$DB2_HOST" "SHOW default_transaction_read_only;")" != "on" ]; then
        log "CRIT: db2 is still writable"
        return 1
    fi
    log "DEMOTE: db2 is now read-only"
 #    # 2. Stop PostgreSQL on DB2
 #    log "DEMOTE: stopping PostgreSQL on db2..."
    if ! podman exec "$DB2_CONTAINER" \
        pg_ctl -D /var/lib/postgresql/data stop -m fast; then
        log "CRIT: failed to stop db2"
        return 1
    fi
 #    # 3. Rewind DB2 so it can follow DB1
 #    log "DEMOTE: rewinding db2 to follow db..."
    if ! podman exec "$DB2_CONTAINER" \
        pg_rewind \
        --target-pgdata=/var/lib/postgresql/data \
        --source-server="host=$DB1_HOST user=$DB_USER dbname=$DB_NAME"; then
        log "WARN: pg_rewind failed"
        log "DEMOTE: db2 may need a full pg_basebackup"
        return 1
    fi
 #    # 4. Configure DB2 as standby
 #    log "DEMOTE: configuring db2 as standby..."
    podman exec "$DB2_CONTAINER" bash -c \
        "touch /var/lib/postgresql/data/standby.signal"
    podman exec "$DB2_CONTAINER" bash -c \
        "echo \"primary_conninfo = 'host=$DB1_HOST user=$DB_USER password=$DB_PASSWORD dbname=$DB_NAME'\" >> /var/lib/postgresql/data/postgresql.auto.conf"
 #    # 5. Start DB2
 #    log "DEMOTE: starting db2 as standby..."
    if ! podman exec "$DB2_CONTAINER" \
        pg_ctl -D /var/lib/postgresql/data start; then
        log "CRIT: failed to start db2"
        return 1
    fi
    # 6. Wait for DB2 to become standby
    for i in {1..20}; do
        sleep 1
        if [ "$(role "$DB2_HOST")" = "standby" ]; then
            log "DEMOTE: db2 is now STANDBY"
            return 0
        fi
        log "DEMOTE: waiting for db2... ($i/20)"
    done
    log "CRIT: db2 did not become STANDBY"
    return 1
}
fence_db() {
    log "FENCE: setting db to read-only (default_transaction_read_only=on)"
   q "$DB1_HOST" "ALTER SYSTEM SET default_transaction_read_only = on;" >/dev/null
    q "$DB1_HOST" "SELECT pg_reload_conf();" >/dev/null
}
FAILS=0
log "============================================================"
log "PostgreSQL HA controller started"
log "db  = $DB1_HOST"
log "db2 = $DB2_HOST"
log "interval      = ${INTERVAL}s"
log "threshold     = ${FAIL_THRESHOLD}"
log "auto promote  = ${AUTO_PROMOTE}"
log "auto fence    = ${AUTO_FENCE}"
log "auto failback = ${AUTO_FAILBACK}"
log "============================================================"
while true; do
    # R1=$(role "$DB1_HOST")
    # R2=$(role "$DB2_HOST")
    # if [ "$R1" = "primary" ]; then
    #     PRIMARY_DB="$DB1_HOST"
    # elif [ "$R2" = "primary" ]; then
    #     PRIMARY_DB="$DB2_HOST"
    # else
    #     PRIMARY_DB=""
    # fi
    # log "Current PRIMARY: ${PRIMARY_DB:-NONE}" 
   if [ "$R1" = "primary" ] && [ "$R2" = "primary" ]; then
       log "CRIT: SPLIT-BRAIN - db AND db2 are both PRIMARY"
        log "CRIT: DO NOT write to both databases."
       if [ "$AUTO_FENCE" = "true" ]; then
            fence_db
        fi
   elif [ "$R1" = "primary" ]; then
       FAILS=0
       ROW=$(repl_info "$DB1_HOST")
        STANDBYS=${ROW%%|*}
        LAG=${ROW##*|}
       if [ "${STANDBYS:-0}" -eq 0 ]; then
            log "WARN: db is PRIMARY but db2 is NOT replicating (db2=$R2)"
        else
            log "OK: db PRIMARY, db2 streaming, lag=${LAG} bytes"
        fi
   elif [ "$R1" = "down" ]; then
       FAILS=$((FAILS + 1))
        log "FAIL: db unreachable ($FAILS/$FAIL_THRESHOLD)"
       case "$R2" in
           standby)
                log "INFO: db2 is reachable as STANDBY"
               if [ "$FAILS" -ge "$FAIL_THRESHOLD" ]; then
                   if [ "$AUTO_PROMOTE" != "true" ]; then
                        log "WARN: AUTO_PROMOTE=false, db2 remains STANDBY"
                   elif db2_still_streaming_from_db; then
                        log "WARN: db2 is still streaming from db - db looks ALIVE"
                          log "WARN: refusing to promote (would cause split-brain)"
                   else
                        log "FAILOVER: db failed, db2 stopped receiving WAL"
                        log "FAILOVER: promoting db2..."
                       if promote_db2; then
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
   elif [ "$R1" = "standby" ] && [ "$R2" = "primary" ]; then
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


# need to add if both of them are not primary make one primary and one standby (standby make the data copied and same accross two servers) , primary would always be there (after getting promoted or existing previously) but the standby is not permanent (if the one goes down the standby gets promoted and the standby spot will be filled by container restart)