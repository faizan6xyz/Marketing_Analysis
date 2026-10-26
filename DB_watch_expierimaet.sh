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
INTERVAL="${CHECK_INTERVAL:-4}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-2}"
AUTO_PROMOTE="${AUTO_PROMOTE:-true}"
AUTO_FENCE="${AUTO_FENCE:-true}"
AUTO_DEMOTE="${AUTO_DEMOTE:-true}"
SYNC_REPL="${SYNC_REPL:-true}"
SYNC_NAMES="${SYNC_NAMES:-*}"
REPOINT_STANDBYS="${REPOINT_STANDBYS:-false}"   # after failover, repoint the other standby's primary_conninfo to the new primary
K8S_LABELS="${K8S_LABELS:-auto}"            # whether to run kubectl
K8S_NAMESPACE="${K8S_NAMESPACE:-default}"
LABEL_KEY="${LABEL_KEY:-primary}"           # pod label key (value is true / false)
DB1_POD="${DB1_POD:-$DB1_HOST}"             # pod names, if they differ from the host names
DB2_POD="${DB2_POD:-$DB2_HOST}"
DB3_POD="${DB3_POD:-$DB3_HOST}"
DB1_YML="${DB1_YML:-}"
DB2_YML="${DB2_YML:-}"
DB3_YML="${DB3_YML:-}"
ACCESS_TOKEN="${ACCESS_TOKEN:-}"
exec 9>/tmp/ha-controller.lock
if ! flock -n 9; then
    echo "$(date '+%F %T') CRIT: another ha-controller instance is already running"
    exit 1
fi
ts()  { date '+%F %T'; }
log() { echo "$(ts) $*"; }
trap 'log "INFO: controller stopped"; exit 0' INT TERM
declare -A K8S_WARNED=()
q() {
    psql -X -h "$1" -U "$DB_USER" -d "$DB_NAME" -Atc "$2" 2>/dev/null
}
role() {
    local host="$1"
    local r
    if [ -z "$host" ]; then
        echo "down"
        return
    fi
    if ! pg_isready -q -t 3 -h "$host" -U "$DB_USER" -d "$DB_NAME"; then
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
    cur=$(q "$host" "SELECT setting FROM pg_settings WHERE name = 'synchronous_standby_names';")
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
pod_of() {
    case "$1" in
        "$DB1_HOST") echo "$DB1_POD" ;;
        "$DB2_HOST") echo "$DB2_POD" ;;
        "$DB3_HOST") echo "$DB3_POD" ;;
        *)           echo "$1" ;;
    esac
}
set_pg_marker() {
    local host="$1" want="$2" cur changed=0
    pg_isready -q -h "$host" -U "$DB_USER" -d "$DB_NAME" || return 0
    cur=$(q "$host" "SELECT current_setting('ha.primary', true);")
    if [ "$want" = "on" ]; then
        if [ "$cur" != "true" ]; then
            log "LABEL: setting ha.primary=true on $host"
            q "$host" "ALTER SYSTEM SET ha.primary = 'true';" >/dev/null && changed=1
        fi
    else
        if [ -n "$cur" ]; then
            log "LABEL: resetting ha.primary on $host"
            q "$host" "ALTER SYSTEM RESET ha.primary;" >/dev/null && changed=1
        fi
    fi
    if [ "$changed" -eq 1 ]; then
        q "$host" "SELECT pg_reload_conf();" >/dev/null
    fi
    return 0
}
set_pod_label() {
    local host="$1" want="$2" pod target cur
    case "$K8S_LABELS" in
        false) return 0 ;;
        auto)  command -v kubectl >/dev/null 2>&1 || return 0 ;;
    esac
    pod=$(pod_of "$host")
    if [ "$want" = "on" ]; then target="true"; else target="false"; fi
    cur=$(kubectl -n "$K8S_NAMESPACE" get pod "$pod" -o "jsonpath={.metadata.labels.${LABEL_KEY}}" 2>/dev/null)
    [ "$cur" = "$target" ] && return 0
    if kubectl -n "$K8S_NAMESPACE" label pod "$pod" "${LABEL_KEY}=${target}" --overwrite >/dev/null 2>&1; then
        log "LABEL: pod $pod -> ${LABEL_KEY}=${target}"
        unset 'K8S_WARNED[$pod]'
    elif [ -z "${K8S_WARNED[$pod]:-}" ]; then
        K8S_WARNED[$pod]=1
        log "WARN: could not label pod '$pod' in namespace '$K8S_NAMESPACE' (check pod name, RBAC, or that this runs against Kubernetes)"
    fi
    return 0
}
set_label() {
    local host="$1" want="$2"
    set_pg_marker "$host" "$want"
    set_pod_label "$host" "$want"
    return 0
}
promote_node() {
    local host="$1"
    local result i
    log "PROMOTE: requesting promotion of $host..."
    result=$(psql -X -w -h "$host" -U "$DB_USER" -d "$DB_NAME" -Atc "SELECT pg_promote();" 2>&1)
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
repoint_standby() {
    local host="$1" new_primary="$2"
    [ "$REPOINT_STANDBYS" = "true" ] || return 0
    [ "$(role "$host")" = "standby" ] || return 0
    log "REPOINT: $host -> follow $new_primary"
    q "$host" "ALTER SYSTEM SET primary_conninfo = 'host=$new_primary port=5432 user=$DB_USER password=$DB_PASSWORD application_name=$host';" >/dev/null
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
    log "DEMOTE: stopping $l_container (clean shutdown, needed by pg_rewind)..."
    if ! podman stop -t 60 "$l_container" >/dev/null; then
        log "CRIT: failed to stop $l_container"
        return 1
    fi
    if [ -n "$PODMAN_NETWORK" ]; then
        net_args=(--network "$PODMAN_NETWORK")
    fi
    log "DEMOTE: rewinding $l_host to follow $w_host..."
    if ! podman run --rm --name "${l_container}-rewind" --user postgres ${net_args[@]+"${net_args[@]}"} -e PGPASSWORD="$DB_PASSWORD" -v "$l_volume:/var/lib/postgresql/data" "$PG_IMAGE" pg_rewind --target-pgdata=/var/lib/postgresql/data --source-server="host=$w_host port=5432 user=$DB_USER dbname=$DB_NAME" --write-recovery-conf --progress ; then
        log "CRIT: pg_rewind failed, $l_container is left STOPPED"
        log "CRIT: rebuild it by hand, e.g. wipe volume $l_volume and run pg_basebackup -R from $w_host"
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
    log "CRIT: SPLIT-BRAIN - $DB1_HOST AND $DB2_HOST are both PRIMARY ,CRIT: DO NOT write to both databases."
    case "$PRIMARY_DB" in
        "$DB1_HOST")
            w_host="$DB1_HOST";l_host="$DB2_HOST"; l_container="$DB2_CONTAINER"; l_volume="$DB2_VOLUME"
            ;;
        "$DB2_HOST")
            w_host="$DB2_HOST";l_host="$DB1_HOST"; l_container="$DB1_CONTAINER"; l_volume="$DB1_VOLUME"
            ;;
        *)
            log "CRIT: last known primary is unknown - refusing to guess which node to demote"
            log "CRIT: restart with PRIMARY_DB=$DB1_HOST or PRIMARY_DB=$DB2_HOST, or demote manually"
            return 1
            ;;
    esac
    log "CRIT: keeping $w_host as PRIMARY, demoting $l_host (its unreplicated writes will be lost)"
    set_label "$l_host" off
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
LAG=0
log "PostgreSQL HA controller started"
log "  db  = $DB1_HOST (container $DB1_CONTAINER, volume $DB1_VOLUME, pod $DB1_POD)"
log "  db2 = $DB2_HOST (container $DB2_CONTAINER, volume $DB2_VOLUME, pod $DB2_POD)"
log "  db3 = $DB3_HOST (container $DB3_CONTAINER, volume $DB3_VOLUME, pod $DB3_POD)"
log "  known primary = ${PRIMARY_DB:-<unknown>}, interval = ${INTERVAL}s, threshold = ${FAIL_THRESHOLD}"
log "  auto promote = ${AUTO_PROMOTE}, fence = ${AUTO_FENCE}, demote = ${AUTO_DEMOTE}, failback = ${AUTO_FAILBACK}"
log "  sync repl = ${SYNC_REPL} (standby names: ${SYNC_NAMES})"
log "  k8s labels = ${K8S_LABELS} (namespace ${K8S_NAMESPACE}, label ${LABEL_KEY}=true|false)"
while true; do
    if ! kubectl get pod "$DB1_POD" >/dev/null 2>&1; then
        echo "db pod missing , Recreating the db "
        curl -H "Authorization: token $ACCESS_TOKEN" -fsSL https://raw.githubusercontent.com/faizan6xyz/Marketing_Analysis/main/minikube/$DB1_YML | kubectl apply -f -
    fi
    if ! kubectl get pod "$DB2_POD" >/dev/null 2>&1; then
        echo "db2 pod missing , Recreating the db2 "
        curl -H "Authorization: token $ACCESS_TOKEN" -fsSL https://raw.githubusercontent.com/faizan6xyz/Marketing_Analysis/main/minikube/$DB2_YML | kubectl apply -f -
    fi
    if ! kubectl get pod "$DB3_POD" >/dev/null 2>&1; then
        echo "db3 pod missing , Recreating the db3 "
        curl -H "Authorization: token $ACCESS_TOKEN" -fsSL https://raw.githubusercontent.com/faizan6xyz/Marketing_Analysis/main/minikube/$DB3_YML | kubectl apply -f -
    fi
    R1=$(role "$DB1_HOST")
    R2=$(role "$DB2_HOST")
    R3=$(role "$DB3_HOST")
    if [ "$R1" = "primary" ] && [ "$R2" = "primary" ]; then
        handle_split_brain
        sleep "$INTERVAL"
        continue
    elif [ "$R1" = "primary" ]; then
        PRIMARY_DB="$DB1_HOST";STANDBY_DB="$DB2_HOST"
    elif [ "$R2" = "primary" ]; then
        PRIMARY_DB="$DB2_HOST";STANDBY_DB="$DB1_HOST"
    # elif in case the PRIMARY_DB variable is empty , btw it wouldn't happen because after the restart the variable will be given be the secrects in which PRIMARY_DB will be defined  . in simple this elif is prepared for the fault  which wouldn't happen
    elif [ -z "$PRIMARY_DB" ]; then
        log "WARN: no primary discovered yet (db=$R1, db2=$R2), waiting"
        if [ "$R1" != "down" ] && [ "$R2" != "down" ]; then
            PRIMARY_DB="$DB1_HOST";STANDBY_DB="$DB2_HOST"
        elif [ "$R1" = "down" ]; then
            PRIMARY_DB="$DB2_HOST";STANDBY_DB="$DB1_HOST"
        elif [ "$R2" = "down" ]; then
            PRIMARY_DB="$DB1_HOST";STANDBY_DB="$DB2_HOST"
        fi
    fi
    health_pm=$(role "$PRIMARY_DB")
    health_st=$(role "$STANDBY_DB")
    if [[ "$health_pm" = "down" && "$health_st" = "standby" ]]; then
        FAILS=$((FAILS + 1))
        log "FAIL: $PRIMARY_DB unreachable ($FAILS/$FAIL_THRESHOLD)"
        log "INFO: $STANDBY_DB is reachable as STANDBY"
        if [ "$FAILS" -ge "$FAIL_THRESHOLD" ]; then
            if [ "$AUTO_PROMOTE" != "true" ]; then
                log "WARN: AUTO_PROMOTE=false, $STANDBY_DB remains STANDBY"
            elif is_streaming "$STANDBY_DB"; then
                log "WARN: $STANDBY_DB is still streaming from $PRIMARY_DB - $PRIMARY_DB looks ALIVE"
                log "WARN: refusing to promote (would cause split-brain)"
            else
                log "FAILOVER: $PRIMARY_DB failed, $STANDBY_DB stopped receiving WAL"
                if [ "$AUTO_FENCE" = "true" ]; then
                    fence_node "$PRIMARY_DB"
                fi
                log "FAILOVER: promoting $STANDBY_DB..."
                if promote_node "$STANDBY_DB"; then
                    old_primary="$PRIMARY_DB";new_primary="$STANDBY_DB";PRIMARY_DB="$new_primary";STANDBY_DB="$old_primary"
                    set_sync "$PRIMARY_DB" off
                    set_label "$STANDBY_DB" off
                    set_label "$DB3_HOST" off
                    set_label "$PRIMARY_DB" on
                    repoint_standby "$DB3_HOST" "$PRIMARY_DB"
                    log "FAILOVER COMPLETE: $new_primary = PRIMARY, $old_primary = FAILED. Repoint apps to $new_primary."
                    FAILS=0
                else
                    log "CRIT: FAILOVER FAILED"
                fi
            fi
        fi
    else
        FAILS=0
    fi
    if [ "$health_pm" = "primary" ]; then
        IFS='|' read -r _ LAG <<< "$(repl_info "$PRIMARY_DB")"
        LAG="${LAG:-0}"
        live=()
        [ "$health_st" = "standby" ] && live+=("$STANDBY_DB")
        [ "$R3" = "standby" ]        && live+=("$DB3_HOST")
        case ${#live[@]} in
            0)
                log "WARN: $PRIMARY_DB is PRIMARY, no standby is up ($STANDBY_DB=$health_st, $DB3_HOST=$R3) - sync replication disabled"
                set_sync "$PRIMARY_DB" off
                ;;
            1)
                if [ "$health_st" != "standby" ]; then
                    log "WARN: $PRIMARY_DB is PRIMARY, $STANDBY_DB is NOT replicating ($STANDBY_DB=$health_st), db3 streaming (sync), lag=${LAG} bytes"
                else
                    log "WARN: $PRIMARY_DB is PRIMARY, $STANDBY_DB streaming (sync), lag=${LAG} bytes, but db3 is NOT replicating (db3=$R3)"
                fi
                SYNC_NAMES="FIRST 1 ${live[0]}"
                set_sync "$PRIMARY_DB" on
                ;;
            *)
                SYNC_NAMES="ANY 1 (${live[0]}, ${live[1]})"
                set_sync "$PRIMARY_DB" on
                ;;
        esac
    fi
    sleep "$INTERVAL"
done


# neet to add the fallback if someone accidently deleted the pod or any of the db 