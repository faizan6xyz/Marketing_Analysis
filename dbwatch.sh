#!/bin/bash
set -u
export PGPASSWORD="${DB_PASSWORD:?DB_PASSWORD is not set}"
export PGCONNECT_TIMEOUT=3
DB1_HOST="${DB1_HOST:-db}"
DB2_HOST="${DB2_HOST:-db2}"
DB3_HOST="${DB3_HOST:-db3}"
DB1_CONTAINER="${DB1_CONTAINER:-$DB1_HOST}"
DB2_CONTAINER="${DB2_CONTAINER:-$DB2_HOST}"
DB3_CONTAINER="${DB3_CONTAINER:-$DB3_HOST}"
DB1_VOLUME="${DB1_VOLUME:-myapp_db_data}"
DB2_VOLUME="${DB2_VOLUME:-myapp_db2_data}"
DB3_VOLUME="${DB3_VOLUME:-myapp_db3_data}"
PODMAN_NETWORK="${PODMAN_NETWORK:-myapp_default}"
PG_IMAGE="${PG_IMAGE:-docker.io/library/postgres:16}"
DB_USER="${DB_USER:-leojr}"
DB_NAME="${DB_NAME:-userdb}"
PRIMARY_DB="${PRIMARY_DB:-}"
STANDBY_DB="${STANDBY_DB:-}"
INTERVAL="${CHECK_INTERVAL:-3}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-5}"
AUTO_PROMOTE="${AUTO_PROMOTE:-true}"
AUTO_FENCE="${AUTO_FENCE:-true}"
AUTO_DEMOTE="${AUTO_DEMOTE:-true}"
AUTO_FAILBACK="${AUTO_FAILBACK:-true}"
FAILBACK_CMD="${FAILBACK_CMD:-}"
SYNC_REPL="${SYNC_REPL:-true}"
SYNC_NAMES="${SYNC_NAMES:-*}"   # '*' = any connected standby
exec 9>/tmp/ha-controller.lock
if ! flock -n 9; then
    echo "$(date '+%F %T') CRIT: another ha-controller instance is already running"
    exit 1
fi
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
standby_name() {
    local name
    name=$(q "$1" "SELECT application_name FROM pg_stat_replication WHERE state = 'streaming' ORDER BY application_name LIMIT 1;")
    [ -n "$name" ] || return 1
    echo "$name"
}
repl_info() {
    local row
    row=$(q "$1" "SELECT count(*), COALESCE(max(pg_wal_lsn_diff(sent_lsn, replay_lsn)), 0)::bigint  FROM pg_stat_replication;")
    echo "${row:-0|0}"
}
is_streaming() {
    local n
    n=$(q "$1" "SELECT count(*) FROM pg_stat_wal_receiver WHERE status = 'streaming';")
    [ "${n:-0}" -ge 1 ]
}
set_sync() {
    local host="$1" want="$2" cur target
    [ "$SYNC_REPL" = "true" ] || return 0
    if [ "$want" = "on" ]; then target="$SYNC_NAMES"; else target=""; fi
    cur=$(q "$host" "SHOW synchronous_standby_names;")
    [ "$cur" = "$target" ] && return 0
    if [ -z "$target" ]; then
        log "SYNC: disabling synchronous replication on $host (no standby)"
        q "$host" "ALTER SYSTEM RESET synchronous_standby_names;" >/dev/null
    else
        log "SYNC: enabling synchronous replication on $host (standbys: $target)"
        q "$host" "ALTER SYSTEM SET synchronous_standby_names = '$target';" >/dev/null
    fi
    q "$host" "SELECT pg_reload_conf();" >/dev/null
}
set_label() {
    local host="$1" want="$2" cur
    cur=$(q "$host" "SHOW ha.primary;")
    if [ "$want" = "on" ]; then
        [ "$cur" = "true" ] && return 0
        log "LABEL: setting primary=true on $host"
        q "$host" "ALTER SYSTEM SET ha.primary = 'true';" >/dev/null
    else
        [ -z "$cur" ] && return 0
        log "LABEL: removing primary label from $host"
        q "$host" "ALTER SYSTEM RESET ha.primary;" >/dev/null
    fi
    q "$host" "SELECT pg_reload_conf();" >/dev/null
}
promote_node() {
    local host="$1"
    local result i
    log "PROMOTE: requesting promotion of $host..."
    result=$(psql -X -h "$host" -U "$DB_USER" -d "$DB_NAME" -Atc "SELECT pg_promote();" 2>&1)
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
fence_node() {
    local host="$1"
    log "FENCE: setting $host to read-only (best effort)"
    q "$host" "ALTER SYSTEM SET default_transaction_read_only = on;" >/dev/null
    q "$host" "SELECT pg_reload_conf();" >/dev/null
}
demote_to_standby() {
    local l_container="$1" l_host="$2" l_volume="$3" w_host="$4"
    local i
    local net_args=()
    log "DEMOTE: turning $l_host into a STANDBY of $w_host..."
    if [ "$(role "$w_host")" != "primary" ]; then
        log "CRIT: $w_host is not primary, refusing to demote $l_host"
        return 1
    fi
    q "$w_host" "CHECKPOINT;" >/dev/null
    log "DEMOTE: stopping $l_container..."
    if ! podman stop -t 60 "$l_container" >/dev/null; then
        log "CRIT: failed to stop $l_container"
        return 1
    fi
    if [ -n "$PODMAN_NETWORK" ]; then
        net_args=(--network "$PODMAN_NETWORK")
    fi
    log "DEMOTE: rewinding $l_host to follow $w_host..."
    if ! podman run --rm --user postgres ${net_args[@]+"${net_args[@]}"} -v "$l_volume":/var/lib/postgresql/data "$PG_IMAGE" pg_rewind --target-pgdata=/var/lib/postgresql/data --source-server="host=$w_host port=5432 user=$DB_USER password=$DB_PASSWORD dbname=$DB_NAME" --write-recovery-conf --progress; then
        log "CRIT: pg_rewind failed, $l_container is left STOPPED , CRIT: rebuild it by hand, e.g. wipe volume $l_volume and run pg_basebackup -R from $w_host"
        return 1
    fi
    log "DEMOTE: starting $l_container as standby..."
    if ! podman start "$l_container" >/dev/null; then
        log "CRIT: failed to start $l_container"
        return 1
    fi
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
    set_label "$l_host" off
    for i in {1..15}; do
        if is_streaming "$l_host"; then
            log "DEMOTE: $l_host is streaming from $w_host"
            return 0
        fi
        sleep 1
    done
    log "CRIT: $l_host is STANDBY but not streaming from $w_host , CRIT: check primary_conninfo, pg_hba.conf and the REPLICATION privilege of $DB_USER"
    return 1
}
handle_split_brain() {
    local w_host l_host l_container l_volume
    log "CRIT: SPLIT-BRAIN - $DB1_HOST AND $DB2_HOST are both PRIMARY , CRIT: DO NOT write to both databases."
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
            log "CRIT: last known primary is unknown - refusing to guess which node to demote , CRIT: restart with PRIMARY_DB=$DB1_HOST or PRIMARY_DB=$DB2_HOST, or demote manually"
            return 1
            ;;
    esac
    log "CRIT: keeping $w_host as PRIMARY, demoting $l_host (its unreplicated writes will be lost)"
    set_label "$w_host" on
    if [ "$AUTO_FENCE" = "true" ]; then
        fence_node "$l_host"
    fi
    if [ "$AUTO_DEMOTE" != "true" ]; then
        log "WARN: AUTO_DEMOTE=false, $l_host left running - demote it manually"
        return 1
    fi
    demote_to_standby "$l_container" "$l_host" "$l_volume" "$w_host"
}
FAILS=0
log "PostgreSQL HA controller started  ,  db  = $DB1_HOST (container $DB1_CONTAINER, volume $DB1_VOLUME)  ,  db2 = $DB2_HOST (container $DB2_CONTAINER, volume $DB2_VOLUME)  ,  known primary = ${PRIMARY_DB:-<unknown>}  ,  interval      = ${INTERVAL}s  ,  threshold     = ${FAIL_THRESHOLD}  ,  auto promote  = ${AUTO_PROMOTE}  ,  auto fence    = ${AUTO_FENCE}  ,  auto demote   = ${AUTO_DEMOTE}  ,  auto failback = ${AUTO_FAILBACK}  ,  sync repl     = ${SYNC_REPL} (standby names: ${SYNC_NAMES})"
while true; do
    R1=$(role "$DB1_HOST")
    R2=$(role "$DB2_HOST")
    R3=$(role "$DB3_HOST")
    if [ "$R1" = "primary" ] && [ "$R2" = "primary" ]; then
        handle_split_brain
    elif [ "$R1" = "primary" ]; then
        PRIMARY_DB="$DB1_HOST"
        FAILS=0
        set_label "$DB1_HOST" on
        [ "$R2" != "down" ] && set_label "$DB2_HOST" off
        ROW=$(repl_info "$DB1_HOST")
        STANDBYS=${ROW%%|*}
        LAG=${ROW##*|}
        if [ "${STANDBYS:-0}" -eq 0 ]; then
            set_sync "$DB1_HOST" off
            log "WARN: db is PRIMARY but db2 is NOT replicating (db2=$R2), db3 is NOT replicating (db3=$R3)"
        elif [ "${STANDBYS:-0}" -eq 1 ]; then
            NAME=$(standby_name "$DB1_HOST") || NAME=""
            case "$NAME" in
                db2)
                    SYNC_NAMES="FIRST 1 (db2)"
                    set_sync "$DB1_HOST" on
                    log "WARN: db is PRIMARY, db2 streaming (sync), lag=${LAG} bytes, but db3 is NOT replicating (db3=$R3)"
                    ;;
                db3)
                    SYNC_NAMES="FIRST 1 (db3)"
                    set_sync "$DB1_HOST" on
                    log "WARN: db is PRIMARY, db2 is NOT replicating (db2=$R2), db3 streaming (sync), lag=${LAG} bytes"
                    ;;
                *)
                    set_sync "$DB1_HOST" off
                    log "WARN: db is PRIMARY, 1 standby streaming but name is '${NAME}' (expected db2 or db3); sync disabled"
                    ;;
            esac
        else
            SYNC_NAMES="FIRST 1 (db2, db3)"
            set_sync "$DB1_HOST" on
            log "OK: db PRIMARY, db2 + db3 streaming, db2 is sync and db3 is the fallback, max lag=${LAG} bytes"
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
                    elif is_streaming "$DB2_HOST"; then
                        log "WARN: db2 is still streaming from db - db looks ALIVE , WARN: refusing to promote (would cause split-brain)"
                    else
                        log "FAILOVER: db failed, db2 stopped receiving WAL , FAILOVER: promoting db2..."
                        if promote_node "$DB2_HOST"; then
                            PRIMARY_DB="$DB2_HOST"
                            set_sync "$DB2_HOST" off
                            set_label "$DB2_HOST" on
                            log "FAILOVER COMPLETE , db2 = PRIMARY , db  = FAILED , Repoint apps to db2."
                            FAILS=0
                        else
                            log "CRIT: FAILOVER FAILED"
                        fi
                    fi
                fi
                ;;
            primary)
                PRIMARY_DB="$DB2_HOST"
                set_sync "$DB2_HOST" off
                set_label "$DB2_HOST" on
                log "OK: db2 is already PRIMARY (serving traffic) , INFO: db is currently unavailable"
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
        PRIMARY_DB="$DB2_HOST"
        FAILS=0
        set_label "$DB2_HOST" on
        set_label "$DB1_HOST" off
        ROW=$(repl_info "$DB2_HOST")
        STANDBYS=${ROW%%|*}
        LAG=${ROW##*|}
        NAMES=$(q "$DB2_HOST" "SELECT COALESCE(string_agg(application_name, ', ' ORDER BY application_name), '') FROM pg_stat_replication WHERE state = 'streaming';")
        if [ "${STANDBYS:-0}" -ge 1 ] && [ -n "$NAMES" ]; then
            SYNC_NAMES="FIRST 1 ($NAMES)"
            set_sync "$DB2_HOST" on
        else
            set_sync "$DB2_HOST" off
        fi
        DB_LAG=$(q "$DB2_HOST" "SELECT COALESCE(max(pg_wal_lsn_diff(sent_lsn, replay_lsn)), -1)::bigint FROM pg_stat_replication WHERE application_name = 'db' AND state = 'streaming';")
        log "INFO: db2 is PRIMARY, standbys streaming=${STANDBYS:-0} (${NAMES:-none}), max lag=${LAG:-?} bytes, db lag=${DB_LAG:-?} bytes"
        if [ "$AUTO_FAILBACK" = "true" ] && [ "${DB_LAG:--1}" -eq 0 ] && [ -n "$FAILBACK_CMD" ]; then
            log "FAILBACK: db is caught up, running FAILBACK_CMD"
            if $FAILBACK_CMD; then
                log "FAILBACK: done. db should be PRIMARY, rebuild db2 as standby."
            else
                log "CRIT: FAILBACK_CMD failed - db2 remains PRIMARY"
            fi
        fi
done