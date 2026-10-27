#!/bin/bashset -u
export PGPASSWORD="${DB_PASSWORD:?DB_PASSWORD is not set}"
export PGCONNECT_TIMEOUT=3
RUNTIME="${RUNTIME:-podman}"
DB1_HOST="${DB1_HOST:-db}"
DB2_HOST="${DB2_HOST:-db2}"
DB3_HOST="${DB3_HOST:-db3}"
DB1_CONTAINER="${DB1_CONTAINER:-$DB1_HOST}"
DB2_CONTAINER="${DB2_CONTAINER:-$DB2_HOST}"
DB3_CONTAINER="${DB3_CONTAINER:-$DB3_HOST}"
DB1_VOLUME="${DB1_VOLUME:-myapp_db_data}"
DB2_VOLUME="${DB2_VOLUME:-myapp_db2_data}"
DB3_VOLUME="${DB3_VOLUME:-myapp_db3_data}"
DB1_POD="${DB1_POD:-$DB1_HOST}"
DB2_POD="${DB2_POD:-$DB2_HOST}"
DB3_POD="${DB3_POD:-$DB3_HOST}"
DB1_YML="${DB1_YML:-}"
DB2_YML="${DB2_YML:-}"
DB3_YML="${DB3_YML:-}"
HOSTS=("$DB1_HOST" "$DB2_HOST" "$DB3_HOST")
CONTAINERS=("$DB1_CONTAINER" "$DB2_CONTAINER" "$DB3_CONTAINER")
VOLUMES=("$DB1_VOLUME" "$DB2_VOLUME" "$DB3_VOLUME")
PODS=("$DB1_POD" "$DB2_POD" "$DB3_POD")
YMLS=("$DB1_YML" "$DB2_YML" "$DB3_YML")
PODMAN_NETWORK="${PODMAN_NETWORK:-myapp_default}"
PG_IMAGE="${PG_IMAGE:-docker.io/library/postgres:16}"
DB_USER="${DB_USER:-leojr}"
DB_NAME="${DB_NAME:-userdb}"
PRIMARY_DB="${PRIMARY_DB:-}"          # bootstrap only; the state file wins once it exists
STATE_FILE="${STATE_FILE:-/var/tmp/ha-controller.state}"   # put this on a persistent path
INTERVAL="${CHECK_INTERVAL:-4}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-2}"
AUTO_PROMOTE="${AUTO_PROMOTE:-true}"
AUTO_FENCE="${AUTO_FENCE:-true}"
AUTO_DEMOTE="${AUTO_DEMOTE:-true}"
REBUILD_ON_FAILURE="${REBUILD_ON_FAILURE:-false}"  # if pg_rewind is impossible, wipe the loser's volume and pg_basebackup it
SYNC_REPL="${SYNC_REPL:-true}"
REPOINT_STANDBYS="${REPOINT_STANDBYS:-true}"
REPOINT_COOLDOWN="${REPOINT_COOLDOWN:-30}"
if [ "$RUNTIME" = "k8s" ]; then K8S_LABELS="${K8S_LABELS:-auto}"; else K8S_LABELS="${K8S_LABELS:-false}"; fi
K8S_NAMESPACE="${K8S_NAMESPACE:-default}"
LABEL_KEY="${LABEL_KEY:-primary}"
DEMOTE_K8S_DELETE_POD="${DEMOTE_K8S_DELETE_POD:-false}"
RECREATE_COOLDOWN="${RECREATE_COOLDOWN:-60}"
MANIFEST_BASE="${MANIFEST_BASE:-https://raw.githubusercontent.com/faizan6xyz/Marketing_Analysis/main/minikube}"  # pin to a commit SHA
ACCESS_TOKEN="${ACCESS_TOKEN:-}"
exec 9>/tmp/ha-controller.lock
if ! flock -n 9; then
    echo "$(date '+%F %T') CRIT: another ha-controller instance is already running"
    exit 1
fi
ts()  { date '+%F %T'; }
log() { echo "$(ts) $*"; }
trap 'log "INFO: controller stopped"; exit 0' INT TERM
declare -A K8S_WARNED=() FENCED=() SYNC_APPLIED=() LAST_REPOINT=() LAST_RECREATE=()
declare -a ROLE=()
SAVED_TL=0
q() {
    printf '%s\n' "$2" | timeout 10 psql -X -h "$1" -U "$DB_USER" -d "$DB_NAME" -Atq 2>/dev/null
}
role() {
    local host="$1" r
    if [ -z "$host" ]; then echo "down"; return; fi
    if ! pg_isready -q -t 3 -h "$host" -U "$DB_USER" -d "$DB_NAME"; then
        echo "down"; return
    fi
    r=$(q "$host" "SELECT pg_is_in_recovery();")
    case "$r" in
        f) echo "primary" ;;
        t) echo "standby" ;;
        *) echo "unknown" ;;
    esac
}
probe_roles() {
    local i tmp
    tmp=$(mktemp -d)
    for i in "${!HOSTS[@]}"; do
        ( role "${HOSTS[$i]}" > "$tmp/$i" ) &
    done
    wait
    for i in "${!HOSTS[@]}"; do
        ROLE[$i]=$(cat "$tmp/$i" 2>/dev/null)
        [ -n "${ROLE[$i]}" ] || ROLE[$i]="down"
    done
    rm -rf "$tmp"
}
idx_of() {
    local i
    for i in "${!HOSTS[@]}"; do
        if [ "${HOSTS[$i]}" = "$1" ]; then echo "$i"; return 0; fi
    done
    return 1
}
timeline() {
    local f
    f=$(q "$1" "SELECT substr(pg_walfile_name(pg_current_wal_lsn()),1,8);")
    if [[ "$f" =~ ^[0-9A-Fa-f]{8}$ ]]; then echo $((16#$f)); else echo 0; fi
}
sysid() { q "$1" "SELECT system_identifier FROM pg_control_system();"; }
lsn_pos() {
    q "$1" "SELECT COALESCE(GREATEST(pg_wal_lsn_diff(pg_last_wal_receive_lsn(),'0/0'), pg_wal_lsn_diff(pg_last_wal_replay_lsn(),'0/0')),0)::bigint;"
}
is_streaming() {
    local n
    n=$(q "$1" "SELECT count(*) FROM pg_stat_wal_receiver WHERE status = 'streaming';")
    [ "${n:-0}" -ge 1 ]
}
streaming_names() {
    q "$1" "SELECT DISTINCT application_name FROM pg_stat_replication WHERE state = 'streaming' ORDER BY 1;"
}
save_state() {
    printf '%s %s\n' "$PRIMARY_DB" "$SAVED_TL" > "${STATE_FILE}.tmp" 2>/dev/null && mv -f "${STATE_FILE}.tmp" "$STATE_FILE"
}
load_state() {
    local s_host="" s_tl=""
    [ -f "$STATE_FILE" ] || return 0
    read -r s_host s_tl < "$STATE_FILE"
    if [ -n "$s_host" ] && idx_of "$s_host" >/dev/null; then
        if [ -n "$PRIMARY_DB" ] && [ "$PRIMARY_DB" != "$s_host" ]; then
            log "WARN: PRIMARY_DB=$PRIMARY_DB from env disagrees with state file ($s_host); using the state file"
        fi
        PRIMARY_DB="$s_host"
        [[ "$s_tl" =~ ^[0-9]+$ ]] && SAVED_TL="$s_tl"
    fi
}
set_sync() {
    local host="$1" target="$2" cur
    [ "$SYNC_REPL" = "true" ] || return 0
    cur=$(q "$host" "SELECT setting FROM pg_settings WHERE name = 'synchronous_standby_names';")
    if [ -z "$target" ]; then
        if [ -z "$cur" ]; then SYNC_APPLIED[$host]=""; return 0; fi
        log "SYNC: disabling synchronous replication on $host (no streaming standby)"
        q "$host" "ALTER SYSTEM RESET synchronous_standby_names;" >/dev/null
    else
        if [ -n "$cur" ] && [ "${SYNC_APPLIED[$host]:-}" = "$target" ]; then return 0; fi
        log "SYNC: $host synchronous_standby_names = $target"
        q "$host" "ALTER SYSTEM SET synchronous_standby_names = '$target';" >/dev/null
    fi
    q "$host" "SELECT pg_reload_conf();" >/dev/null
    SYNC_APPLIED[$host]="$target"
}
pod_of() {
    local i
    if i=$(idx_of "$1"); then echo "${PODS[$i]}"; else echo "$1"; fi
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
    if [ "$changed" -eq 1 ]; then q "$host" "SELECT pg_reload_conf();" >/dev/null; fi
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
        unset "K8S_WARNED[$pod]"
    elif [ -z "${K8S_WARNED[$pod]:-}" ]; then
        K8S_WARNED[$pod]=1
        log "WARN: could not label pod '$pod' in namespace '$K8S_NAMESPACE' (check pod name, RBAC)"
    fi
    return 0
}
set_label() {
    set_pg_marker "$1" "$2"
    set_pod_label "$1" "$2"
    return 0
}
fence_node() {
    local host="$1"
    FENCED[$host]=1      # remembered: re-applied when the node comes back
    log "FENCE: setting $host to read-only (best effort)"
    q "$host" "ALTER SYSTEM SET default_transaction_read_only = on;" >/dev/null
    q "$host" "SELECT pg_reload_conf();" >/dev/null
}
unfence_node() {
    local host="$1"
    unset "FENCED[$host]"
    if [ "$(q "$host" "SHOW default_transaction_read_only;")" = "on" ]; then
        log "FENCE: removing read-only fence on $host"
        q "$host" "ALTER SYSTEM RESET default_transaction_read_only;" >/dev/null
        q "$host" "SELECT pg_reload_conf();" >/dev/null
    fi
}
enforce_fences() {
    local i h
    for i in "${!HOSTS[@]}"; do
        h="${HOSTS[$i]}"
        [ -n "${FENCED[$h]:-}" ] || continue
        [ "${ROLE[$i]}" = "down" ] && continue
        if [ "$h" = "$PRIMARY_DB" ]; then
            unfence_node "$h"
        elif [ "$(q "$h" "SHOW default_transaction_read_only;")" != "on" ]; then
            fence_node "$h"
        fi
    done
}
promote_node() {
    local host="$1" result i
    log "PROMOTE: requesting promotion of $host..."
    result=$(printf 'SELECT pg_promote();\n' | timeout 15 psql -X -w -h "$host" -U "$DB_USER" -d "$DB_NAME" -Atq 2>&1)
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
repoint_standby() {
    local host="$1" new_primary="$2" force="${3:-}" now cpw conninfo
    [ "$REPOINT_STANDBYS" = "true" ] || [ "$force" = "force" ] || return 0
    [ "$(role "$host")" = "standby" ] || return 0
    now=$(date +%s)
    if [ "$force" != "force" ] && [ $(( now - ${LAST_REPOINT[$host]:-0} )) -lt "$REPOINT_COOLDOWN" ]; then
        return 0
    fi
    LAST_REPOINT[$host]=$now
    log "REPOINT: $host -> follow $new_primary"
    cpw="${DB_PASSWORD//\\/\\\\}"; cpw="${cpw//\'/\\\'}"
    conninfo="host=$new_primary port=5432 user=$DB_USER password='$cpw' application_name=$host"
    conninfo="${conninfo//\'/\'\'}"
    q "$host" "ALTER SYSTEM SET primary_conninfo = '$conninfo';" >/dev/null
    q "$host" "SELECT pg_reload_conf();" >/dev/null
}
pod_missing() {
    local out
    out=$(kubectl -n "$K8S_NAMESPACE" get pod "$1" -o name 2>&1) && return 1
    [[ "$out" == *NotFound* ]]       # API errors / RBAC / no kubectl are NOT "missing"
}
recreate_pod() {
    local i="$1" pod="${PODS[$1]}" yml="${YMLS[$1]}" now tmp
    now=$(date +%s)
    [ $(( now - ${LAST_RECREATE[$pod]:-0} )) -lt "$RECREATE_COOLDOWN" ] && return 0
    LAST_RECREATE[$pod]=$now
    if [ -z "$yml" ]; then
        log "CRIT: pod $pod is missing but DB$((i+1))_YML is not set"
        return 0
    fi
    if [ -z "$ACCESS_TOKEN" ]; then
        log "CRIT: pod $pod is missing but ACCESS_TOKEN is not set"
        return 0
    fi
    log "WARN: pod $pod not found in namespace $K8S_NAMESPACE, re-creating from $yml"
    tmp=$(mktemp)
    if printf 'header = "Authorization: token %s"\n' "$ACCESS_TOKEN" \
        | curl -K - -fsSL -o "$tmp" "$MANIFEST_BASE/$yml" && [ -s "$tmp" ]; then
        kubectl -n "$K8S_NAMESPACE" apply -f "$tmp" || log "CRIT: kubectl apply failed for $pod"
    else
        log "CRIT: could not download manifest $yml"
    fi
    rm -f "$tmp"
}
rebuild_standby() {
    local l_container="$1" l_host="$2" l_volume="$3" w_host="$4"
    local net_args=()
    if [ "$REBUILD_ON_FAILURE" != "true" ]; then
        log "CRIT: $l_container is left STOPPED. Rebuild by hand: wipe volume $l_volume and run pg_basebackup -R from $w_host (or set REBUILD_ON_FAILURE=true)"
        return 1
    fi
    [ -n "$PODMAN_NETWORK" ] && net_args=(--network "$PODMAN_NETWORK")
    log "REBUILD: wiping $l_volume and running pg_basebackup from $w_host"
    podman run --rm --name "${l_container}-rebuild" --user postgres ${net_args[@]+"${net_args[@]}"} \
        -e PGPASSWORD -v "$l_volume:/var/lib/postgresql/data" "$PG_IMAGE" \
        sh -c 'find /var/lib/postgresql/data -mindepth 1 -delete && pg_basebackup -h "$1" -U "$2" -D /var/lib/postgresql/data -X stream -R -P' \
        sh "$w_host" "$DB_USER" || { log "CRIT: pg_basebackup failed, $l_container left STOPPED"; return 1; }
}
demote_to_standby() {
    local idx="$1" w_host="$2"
    local l_host="${HOSTS[$idx]}" l_container="${CONTAINERS[$idx]}" l_volume="${VOLUMES[$idx]}"
    local i sid_l sid_w net_args=()
    log "DEMOTE: turning $l_host into a STANDBY of $w_host..."
    if [ "$(role "$w_host")" != "primary" ]; then
        log "CRIT: $w_host is not primary, refusing to demote $l_host"
        return 1
    fi
    if [ "$RUNTIME" = "k8s" ]; then
        if [ "$DEMOTE_K8S_DELETE_POD" = "true" ]; then
            log "DEMOTE: deleting pod ${PODS[$idx]}; its init step must rebuild it as a standby of $w_host (it stays read-only until then)"
            kubectl -n "$K8S_NAMESPACE" delete pod "${PODS[$idx]}" --wait=false >/dev/null 2>&1 || log "WARN: pod delete failed"
            return 0
        fi
        log "CRIT: RUNTIME=k8s and DEMOTE_K8S_DELETE_POD=false - $l_host stays fenced (read-only); demote it manually"
        return 1
    fi
    sid_l=$(sysid "$l_host"); sid_w=$(sysid "$w_host")
    q "$w_host" "CHECKPOINT;" >/dev/null
    log "DEMOTE: stopping $l_container (clean shutdown, needed by pg_rewind)..."
    if ! podman stop -t 60 "$l_container" >/dev/null; then
        log "CRIT: failed to stop $l_container"
        return 1
    fi
    [ -n "$PODMAN_NETWORK" ] && net_args=(--network "$PODMAN_NETWORK")
    if [ -n "$sid_l" ] && [ -n "$sid_w" ] && [ "$sid_l" != "$sid_w" ]; then
        log "CRIT: $l_host has a different system identifier than $w_host (blank or foreign cluster) - pg_rewind cannot work"
        rebuild_standby "$l_container" "$l_host" "$l_volume" "$w_host" || return 1
    else
        log "DEMOTE: rewinding $l_host to follow $w_host..."
        if ! podman run --rm --name "${l_container}-rewind" --user postgres ${net_args[@]+"${net_args[@]}"} \
            -e PGPASSWORD -v "$l_volume:/var/lib/postgresql/data" "$PG_IMAGE" \
            pg_rewind --target-pgdata=/var/lib/postgresql/data \
            --source-server="host=$w_host port=5432 user=$DB_USER dbname=$DB_NAME" \
            --write-recovery-conf --progress; then
            log "CRIT: pg_rewind failed (needs wal_log_hints=on or data checksums on both nodes)"
            rebuild_standby "$l_container" "$l_host" "$l_volume" "$w_host" || return 1
        fi
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
    repoint_standby "$l_host" "$w_host" force     # sets application_name = host

    for i in {1..15}; do
        if is_streaming "$l_host"; then
            log "DEMOTE: $l_host is streaming from $w_host"
            return 0
        fi
        sleep 1
    done
    log "CRIT: $l_host is STANDBY but not streaming from $w_host - check primary_conninfo, pg_hba.conf and the REPLICATION privilege of $DB_USER"
    return 1
}
handle_split_brain() {
    local -a cand=("$@")
    local -A tls=()
    local i best=-1 best_tl=-1 tie=0 t names=""
    for i in "${cand[@]}"; do names="$names ${HOSTS[$i]}"; done
    log "CRIT: SPLIT-BRAIN - these nodes are all PRIMARY:$names. DO NOT write to more than one."
    for i in "${cand[@]}"; do
        t=$(timeline "${HOSTS[$i]}"); tls[$i]=$t
        log "SPLIT-BRAIN: ${HOSTS[$i]} timeline=$t"
        if [ "$t" -gt "$best_tl" ]; then best=$i; best_tl=$t; tie=0
        elif [ "$t" -eq "$best_tl" ]; then tie=1; fi
    done
    if [ "$best_tl" -le 0 ]; then
        log "CRIT: cannot read timelines - refusing to guess"
        return 1
    fi
    if [ "$tie" -eq 1 ]; then
        best=-1
        for i in "${cand[@]}"; do
            if [ "${tls[$i]}" -eq "$best_tl" ] && [ "${HOSTS[$i]}" = "$PRIMARY_DB" ]; then best=$i; fi
        done
        if [ "$best" -lt 0 ]; then
            log "CRIT: equal timelines and last known primary '${PRIMARY_DB:-<unknown>}' is not among the tied nodes - demote one manually"
            return 1
        fi
        log "WARN: equal timelines, keeping last known primary ${HOSTS[$best]}"
    fi
    log "CRIT: keeping ${HOSTS[$best]} as PRIMARY (timeline $best_tl); the others lose their unreplicated writes"
    PRIMARY_DB="${HOSTS[$best]}"; SAVED_TL="$best_tl"; save_state
    unfence_node "$PRIMARY_DB"
    set_label "$PRIMARY_DB" on

    for i in "${cand[@]}"; do
        [ "$i" -eq "$best" ] && continue
        set_label "${HOSTS[$i]}" off
        [ "$AUTO_FENCE" = "true" ] && fence_node "${HOSTS[$i]}"
        if [ "$AUTO_DEMOTE" != "true" ]; then
            log "WARN: AUTO_DEMOTE=false, ${HOSTS[$i]} left running - demote it manually"
            continue
        fi
        demote_to_standby "$i" "$PRIMARY_DB"
    done
    return 0
}
load_state
FAILS=0
log "PostgreSQL HA controller started (runtime=$RUNTIME)"
for i in "${!HOSTS[@]}"; do
    log "  node$((i+1)) = ${HOSTS[$i]} (container ${CONTAINERS[$i]}, volume ${VOLUMES[$i]}, pod ${PODS[$i]})"
done
log "  known primary = ${PRIMARY_DB:-<unknown>} (timeline $SAVED_TL), interval = ${INTERVAL}s, threshold = ${FAIL_THRESHOLD}"
log "  promote = ${AUTO_PROMOTE}, fence = ${AUTO_FENCE}, demote = ${AUTO_DEMOTE}, rebuild = ${REBUILD_ON_FAILURE}, repoint = ${REPOINT_STANDBYS}"
log "  sync repl = ${SYNC_REPL}, k8s labels = ${K8S_LABELS} (namespace ${K8S_NAMESPACE}, label ${LABEL_KEY}=true|false)"
while true; do
    if [ "$RUNTIME" = "k8s" ] && command -v kubectl >/dev/null 2>&1; then
        for i in "${!PODS[@]}"; do
            if pod_missing "${PODS[$i]}"; then recreate_pod "$i"; fi
        done
    fi
    probe_roles
    primaries=(); standbys=()
    for i in "${!HOSTS[@]}"; do
        case "${ROLE[$i]}" in
            primary) primaries+=("$i") ;;
            standby) standbys+=("$i") ;;
        esac
    done
    enforce_fences
    # ---- 2+ primaries ----
    if [ "${#primaries[@]}" -ge 2 ]; then
        handle_split_brain "${primaries[@]}"
        sleep "$INTERVAL"
        continue
    fi
    # ---- exactly 1 primary: accept it unless it is provably stale ----
    if [ "${#primaries[@]}" -eq 1 ]; then
        p="${primaries[0]}"; ph="${HOSTS[$p]}"
        ptl=$(timeline "$ph")
        if [ "$ptl" -gt 0 ] && [ "$ptl" -lt "$SAVED_TL" ]; then
            log "CRIT: $ph is PRIMARY on timeline $ptl but timeline $SAVED_TL was already used (last primary ${PRIMARY_DB}) - STALE primary, not adopting"
            log "CRIT: delete $STATE_FILE only if you are sure $ph holds the right data"
            [ "$AUTO_FENCE" = "true" ] && fence_node "$ph"
            set_label "$ph" off
            primaries=()
        else
            if [ "$ph" != "$PRIMARY_DB" ] || [ "$ptl" -gt "$SAVED_TL" ]; then
                [ "$ph" != "$PRIMARY_DB" ] && log "INFO: primary is now $ph"
                PRIMARY_DB="$ph"; SAVED_TL="$ptl"; save_state
            fi
            FAILS=0
        fi
    fi
    # ---- no usable primary: failover ----
    if [ "${#primaries[@]}" -eq 0 ]; then
        FAILS=$((FAILS + 1))
        log "FAIL: no usable primary (${HOSTS[0]}=${ROLE[0]}, ${HOSTS[1]}=${ROLE[1]}, ${HOSTS[2]}=${ROLE[2]}) ($FAILS/$FAIL_THRESHOLD)"
        if [ "$FAILS" -ge "$FAIL_THRESHOLD" ]; then
            if [ "$AUTO_PROMOTE" != "true" ]; then
                log "WARN: AUTO_PROMOTE=false, nothing promoted"
            elif [ "${#standbys[@]}" -eq 0 ]; then
                log "CRIT: no standby is up, nothing to promote"
            else
                alive=0
                for i in "${standbys[@]}"; do
                    if is_streaming "${HOSTS[$i]}"; then
                        log "WARN: ${HOSTS[$i]} is still streaming - a primary looks ALIVE, refusing to promote (would cause split-brain)"
                        alive=1
                    fi
                done
                if [ "$alive" -eq 0 ]; then
                    best_host=""; best_pos=-1
                    for i in "${standbys[@]}"; do
                        pos=$(lsn_pos "${HOSTS[$i]}"); pos="${pos:-0}"
                        log "FAILOVER: candidate ${HOSTS[$i]} wal position=$pos"
                        if [ "$pos" -gt "$best_pos" ]; then best_pos="$pos"; best_host="${HOSTS[$i]}"; fi
                    done
                    old_primary="$PRIMARY_DB"
                    log "FAILOVER: ${old_primary:-<unknown>} failed, promoting $best_host (most advanced standby)"
                    if [ -n "$old_primary" ] && [ "$old_primary" != "$best_host" ] && [ "$AUTO_FENCE" = "true" ]; then
                        fence_node "$old_primary"
                    fi
                    if promote_node "$best_host"; then
                        PRIMARY_DB="$best_host"
                        unfence_node "$PRIMARY_DB"
                        SAVED_TL=$(timeline "$PRIMARY_DB"); save_state
                        set_sync "$PRIMARY_DB" ""
                        for h in "${HOSTS[@]}"; do
                            [ "$h" = "$PRIMARY_DB" ] && continue
                            set_label "$h" off
                            repoint_standby "$h" "$PRIMARY_DB"
                        done
                        set_label "$PRIMARY_DB" on
                        log "FAILOVER COMPLETE: $PRIMARY_DB = PRIMARY (timeline $SAVED_TL), ${old_primary:-<unknown>} = FAILED. Repoint apps to $PRIMARY_DB."
                        FAILS=0
                    else
                        log "CRIT: FAILOVER FAILED"
                    fi
                fi
            fi
        fi
        sleep "$INTERVAL"
        continue
    fi
    # ---- healthy primary: keep replication state tidy ----
    mapfile -t names < <(streaming_names "$PRIMARY_DB" | sed '/^$/d')
    lag=$(q "$PRIMARY_DB" "SELECT COALESCE(max(pg_wal_lsn_diff(sent_lsn, replay_lsn)),0)::bigint FROM pg_stat_replication;")
    lag="${lag:-0}"
    # standbys that are up but not streaming: point them at the current primary
    for i in "${!HOSTS[@]}"; do
        [ "${ROLE[$i]}" = "standby" ] || continue
        if ! is_streaming "${HOSTS[$i]}"; then
            log "WARN: ${HOSTS[$i]} is a standby but NOT streaming from $PRIMARY_DB"
            repoint_standby "${HOSTS[$i]}" "$PRIMARY_DB"
        fi
    done
    # synchronous_standby_names only from standbys that are really streaming from this primary
    case "${#names[@]}" in
        0)
            log "WARN: $PRIMARY_DB is PRIMARY with no streaming standby - sync replication disabled"
            set_sync "$PRIMARY_DB" ""
            ;;
        *)
            list=""
            for n in "${names[@]}"; do list="${list:+$list, }\"$n\""; done
            [ "${#names[@]}" -eq 1 ] && log "WARN: only one standby streaming (${names[0]}), lag=${lag} bytes"
            set_sync "$PRIMARY_DB" "ANY 1 ($list)"
            ;;
    esac 
    sleep "$INTERVAL"
done
