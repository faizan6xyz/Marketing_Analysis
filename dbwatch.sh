#!/bin/bash
set -u
export PGPASSWORD="${DB_PASSWORD:?DB_PASSWORD is not set}"
export PGCONNECT_TIMEOUT=3
DB1_HOST="${DB1_HOST:-db}"
DB2_HOST="${DB2_HOST:-db2}"
DB3_HOST="${DB3_HOST:-db3}"
DB_USER="${DB_USER:-leojr}"
DB_NAME="${DB_NAME:-userdb}"
PRIMARY_DB="${PRIMARY_DB:-}"
INTERVAL="${CHECK_INTERVAL:-3}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"
QUERY_TIMEOUT="${QUERY_TIMEOUT:-5}"          # hard cap on every psql call so a hung node can't stall the loop
AUTO_PROMOTE="${AUTO_PROMOTE:-true}"
AUTO_FENCE="${AUTO_FENCE:-true}"
AUTO_DEMOTE="${AUTO_DEMOTE:-true}"
SAVE_DIVERGENT="${SAVE_DIVERGENT:-true}"    # pg_dump the loser before a split-brain demote
SYNC_REPL="${SYNC_REPL:-true}"
REPOINT_STANDBYS="${REPOINT_STANDBYS:-true}" # keep every standby's primary_conninfo pointing at the current primary
REPOINT_COOLDOWN="${REPOINT_COOLDOWN:-30}"
STANDBY_PASSFILE="${STANDBY_PASSFILE:-/etc/pg/pgpass}"   # passfile path as seen by the standby's postgres (mode 0600)
K8S_LABELS="${K8S_LABELS:-auto}"            # whether to run kubectl
K8S_NAMESPACE="${K8S_NAMESPACE:-leo-social}"
LABEL_KEY="${LABEL_KEY:-primary}"           # pod label key (value is true / false)
DB1_POD="${DB1_POD:-db}"             # pod names, if they differ from the host names
DB2_POD="${DB2_POD:-db2}"
DB3_POD="${DB3_POD:-db3}"
DB1_YML="${DB1_YML:-db1_state}"                      # manifest file names, which are in the github repo
DB2_YML="${DB2_YML:-db2_state}"
DB3_YML="${DB3_YML:-db3_state}"
MANIFEST_BASE="${MANIFEST_BASE:-https://raw.githubusercontent.com/faizan6xyz/Marketing_Analysis/main/minikube}"  # pin to a commit SHA
ACCESS_TOKEN="${ACCESS_TOKEN:-}"
RECREATE_COOLDOWN="${RECREATE_COOLDOWN:-30}"
REWIND_MARKER="${REWIND_MARKER:-/var/lib/postgresql/data/.rewind_from}"  # file the initContainer reads to know who to rewind from
STATE_DIR="${STATE_DIR:-/var/lib/ha-controller}"
HOSTS=("$DB1_HOST" "$DB2_HOST" "$DB3_HOST")
mkdir -p "$STATE_DIR" 2>/dev/null || STATE_DIR="/tmp"
STATE_FILE="${STATE_FILE:-$STATE_DIR/primary}"
FENCED_FILE="${FENCED_FILE:-$STATE_DIR/fenced}"
exec 9>/tmp/ha-controller.lock
if ! flock -n 9; then
    echo "$(date '+%F %T') CRIT: another ha-controller instance is already running"
    exit 1
fi
ts()  { date '+%F %T'; }
log() { echo "$(ts) $*"; }
STOP=0
trap 'STOP=1' INT TERM
declare -A K8S_WARNED=()
declare -A LAST_RECREATE=()
declare -A LAST_REPOINT=()
declare -A ROLE=()
primaries=()
standbys=()
LAST_N=-1
REWIND_CHECKED=0
nap() {
    local p
    sleep "$1" & p=$!
    wait "$p" 2>/dev/null
    kill "$p" 2>/dev/null
    return 0
}
q() {
    timeout "$QUERY_TIMEOUT" psql -X -w -h "$1" -U "$DB_USER" -d "$DB_NAME" -Atc "$2" 2>/dev/null
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
refresh_roles() {
    local h tmp pids=()
    tmp=$(mktemp -d)
    for h in "${HOSTS[@]}"; do
        ( role "$h" > "$tmp/$h" ) &
        pids+=("$!")
    done
    wait "${pids[@]}" 2>/dev/null
    for h in "${HOSTS[@]}"; do
        ROLE[$h]=$(cat "$tmp/$h" 2>/dev/null)
        [ -n "${ROLE[$h]}" ] || ROLE[$h]="down"
    done
    rm -rf "$tmp"
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
save_primary() {
    echo "$1" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
}
load_primary() {
    if [ -z "$PRIMARY_DB" ] && [ -f "$STATE_FILE" ]; then
        PRIMARY_DB=$(<"$STATE_FILE")
        log "INFO: loaded last known primary '$PRIMARY_DB' from $STATE_FILE"
    fi
}
set_sync_value() {
    local host="$1" target="$2" cur
    [ "$SYNC_REPL" = "true" ] || return 0
    cur=$(q "$host" "SELECT setting FROM pg_settings WHERE name = 'synchronous_standby_names';")
    [ "$cur" = "$target" ] && return 0
    if [ -z "$target" ]; then
        log "SYNC: disabling synchronous replication on $host (no streaming standby)"
        q "$host" "ALTER SYSTEM RESET synchronous_standby_names;" >/dev/null
    else
        log "SYNC: setting synchronous_standby_names on $host to: $target"
        q "$host" "ALTER SYSTEM SET synchronous_standby_names = '$target';" >/dev/null
    fi
    q "$host" "SELECT pg_reload_conf();" >/dev/null
}
sync_reconcile() {
    local host="$1" names n lag
    names=$(q "$host" "SELECT string_agg(quote_ident(application_name), ', ' ORDER BY application_name) FROM pg_stat_replication WHERE state = 'streaming';")
    n=$(q "$host" "SELECT count(*) FROM pg_stat_replication WHERE state = 'streaming';")
    n="${n:-0}"
    IFS='|' read -r _ lag <<< "$(repl_info "$host")"
    if [ "$n" != "$LAST_N" ]; then
        log "INFO: $host is PRIMARY, $n standby(s) streaming ($names), lag=${lag:-0} bytes"
        LAST_N="$n"
    fi
    case "$n" in
        0) set_sync_value "$host" "" ;;
        1) set_sync_value "$host" "FIRST 1 ($names)" ;;
        *) set_sync_value "$host" "ANY 1 ($names)" ;;
    esac
}
pod_of() {
    case "$1" in
        "$DB1_HOST") echo "$DB1_POD" ;;
        "$DB2_HOST") echo "$DB2_POD" ;;
        "$DB3_HOST") echo "$DB3_POD" ;;
        *)           echo "$1" ;;
    esac
}
yml_of() {
    case "$1" in
        "$DB1_HOST") echo "$DB1_YML" ;;
        "$DB2_HOST") echo "$DB2_YML" ;;
        "$DB3_HOST") echo "$DB3_YML" ;;
        *)           echo "" ;;
    esac
}
k8s_enabled() {
    case "$K8S_LABELS" in
        false) return 1 ;;
        auto)  command -v kubectl >/dev/null 2>&1 || return 1 ;;
    esac
    return 0
}
reconcile_pod() {
    local host="$1" pod yml out now tmp
    k8s_enabled || return 0
    pod=$(pod_of "$host")
    yml=$(yml_of "$host")
    [ -n "$yml" ] || return 0
    out=$(kubectl -n "$K8S_NAMESPACE" get pod "$pod" -o name 2>&1) && return 0
    case "$out" in
        *NotFound*) ;;
        *) log "WARN: cannot query pod $pod (not recreating): $out"; return 0 ;;
    esac
    now=$(date +%s)
    if [ $((now - ${LAST_RECREATE[$pod]:-0})) -lt "$RECREATE_COOLDOWN" ]; then
        return 0
    fi
    LAST_RECREATE[$pod]=$now
    log "WARN: pod $pod is missing, pulling $yml from GitHub and applying"
    tmp=$(mktemp) || { log "WARN: mktemp failed for $pod"; return 0; }
    if printf 'header = "Authorization: token %s"\n' "$ACCESS_TOKEN" | curl -K - -fsSL --retry 3 --retry-delay 2 --max-time 30 -o "$tmp" "$MANIFEST_BASE/$yml" && [ -s "$tmp" ]; then
        if kubectl -n "$K8S_NAMESPACE" apply -f "$tmp" >/dev/null 2>&1; then
            kubectl -n "$K8S_NAMESPACE" label pod "$pod" "${LABEL_KEY}=false" --overwrite >/dev/null 2>&1
        else
            log "WARN: kubectl apply failed for $pod"
        fi
    else
        log "WARN: failed to fetch $yml from GitHub for $pod"
    fi
    rm -f "$tmp"
    return 0
}
set_pg_marker() {
    local host="$1" want="$2" cur changed=0
    pg_isready -q -t 3 -h "$host" -U "$DB_USER" -d "$DB_NAME" || return 0
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
    k8s_enabled || return 0
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
labels_reconcile() {
    local h
    for h in "${HOSTS[@]}"; do
        if [ "$h" = "$PRIMARY_DB" ] && [ "${ROLE[$h]}" = "primary" ]; then
            set_label "$h" on
        else
            set_label "$h" off
        fi
    done
}
fence_node() {
    local host="$1"
    grep -qx "$host" "$FENCED_FILE" 2>/dev/null || echo "$host" >> "$FENCED_FILE"
    set_pod_label "$host" off
    # make a fence for the write queries so it just only accept the read queries only , none of the write queries 
    if q "$host" "ALTER SYSTEM SET default_transaction_read_only = on;" >/dev/null \
       && q "$host" "SELECT pg_reload_conf();" >/dev/null; then
        log "FENCE: $host set to read-only"
        sleep 1
        q "$host" "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid();" >/dev/null
    else
        log "WARN: could not fence $host (down?) - recorded, will retry when it is reachable"
    fi
}
unfence_node() {
    local host="$1"
    # remove a fence for the write queries so it accept the read and write queries 
    q "$host" "ALTER SYSTEM RESET default_transaction_read_only;" >/dev/null
    q "$host" "SELECT pg_reload_conf();" >/dev/null
    sed -i "/^${host}\$/d" "$FENCED_FILE" 2>/dev/null
    return 0
}
fence_reconcile() {
    local h cur fenced=()
    [ "$AUTO_FENCE" = "true" ] || return 0
    [ -f "$FENCED_FILE" ] || return 0
    mapfile -t fenced < "$FENCED_FILE"
    for h in ${fenced[@]+"${fenced[@]}"}; do
        [ -n "$h" ] || continue
        if [ "$h" = "$PRIMARY_DB" ]; then
            unfence_node "$h"
            continue
        fi
        case "${ROLE[$h]:-down}" in
            down) continue ;;
            standby)
                unfence_node "$h"
                continue
                ;;
        esac
        cur=$(q "$h" "SHOW default_transaction_read_only;")
        if [ "$cur" != "on" ]; then
            fence_node "$h"
        fi
    done
}
set_conninfo() {
    local host="$1" new_primary="$2" want cur
    want="host=$new_primary port=5432 user=$DB_USER passfile=$STANDBY_PASSFILE application_name=$host"
    cur=$(q "$host" "SELECT setting FROM pg_settings WHERE name = 'primary_conninfo';")
    [ "$cur" = "$want" ] && return 0
    log "REPOINT: $host -> follow $new_primary"
    q "$host" "ALTER SYSTEM SET primary_conninfo = '$want';" >/dev/null
    q "$host" "SELECT pg_reload_conf();" >/dev/null
}
repoint_reconcile() {
    local primary="$1" names h now
    [ "$REPOINT_STANDBYS" = "true" ] || return 0
    names=$(q "$primary" "SELECT string_agg(application_name, ' ') FROM pg_stat_replication WHERE state = 'streaming';")
    now=$(date +%s)
    for h in "${HOSTS[@]}"; do
        [ "$h" = "$primary" ] && continue
        [ "${ROLE[$h]}" = "standby" ] || continue
        case " $names " in *" $h "*) continue ;; esac
        if [ $((now - ${LAST_REPOINT[$h]:-0})) -lt "$REPOINT_COOLDOWN" ]; then
            continue
        fi
        LAST_REPOINT[$h]=$now
        set_conninfo "$h" "$primary"
    done
}
promote_node() {
    local host="$1"
    local result i
    unfence_node "$host"
    log "PROMOTE: requesting promotion of $host..."
    result=$(timeout 15 psql -X -w -h "$host" -U "$DB_USER" -d "$DB_NAME" -Atc "SELECT pg_promote();" 2>&1)
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
demote_to_standby() {
    local l_host="$1" w_host="$2" pod i owner grace
    pod=$(pod_of "$l_host")
    log "DEMOTE: turning $l_host into a STANDBY of $w_host..."
    if [ "$(role "$w_host")" != "primary" ]; then
        log "CRIT: $w_host is not primary, refusing to demote $l_host"
        return 1
    fi
    if ! k8s_enabled; then
        log "CRIT: kubectl is not available, cannot demote $l_host - demote it manually"
        return 1
    fi
    owner=$(kubectl -n "$K8S_NAMESPACE" get pod "$pod" -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null)
    if [ -z "$owner" ]; then
        log "CRIT: pod $pod has no controller (StatefulSet/Deployment) - deleting it would not recreate it. Demote $l_host manually."
        return 1
    fi
    grace=$(kubectl -n "$K8S_NAMESPACE" get pod "$pod" -o jsonpath='{.spec.terminationGracePeriodSeconds}' 2>/dev/null)
    if [ "${grace:-30}" -lt 60 ]; then
        log "WARN: pod $pod terminationGracePeriodSeconds=${grace:-30}; set it to 120+ or pg_rewind may refuse an unclean shutdown"
    fi
    q "$w_host" "CHECKPOINT;" >/dev/null
    log "DEMOTE: leaving rewind marker for $pod (source: $w_host)"
    if ! kubectl -n "$K8S_NAMESPACE" exec "$pod" -- sh -c "echo '$w_host' > '$REWIND_MARKER'" >/dev/null 2>&1; then
        log "CRIT: could not write rewind marker in $pod, refusing to delete it"
        return 1
    fi
    log "DEMOTE: deleting pod $pod (clean shutdown, initContainer will pg_rewind on restart)..."
    if ! kubectl -n "$K8S_NAMESPACE" delete pod "$pod" --wait=true --timeout=180s >/dev/null 2>&1; then
        log "CRIT: failed to delete pod $pod"
        return 1
    fi
    for i in {1..90}; do
        sleep 2
        if [ "$(role "$l_host")" = "standby" ]; then
            log "DEMOTE: $l_host is now STANDBY"
            break
        fi
        log "DEMOTE: waiting for $l_host... ($i/90)"
        if [ "$i" -eq 90 ]; then
            log "CRIT: $l_host did not become STANDBY, check the pg-rewind initContainer logs of $pod"
            return 1
        fi
    done
    set_conninfo "$l_host" "$w_host"
    unfence_node "$l_host"
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
pick_winner() {
    local h t l best="" best_tl=-1 best_lsn=-1
    for h in "$@"; do
        if [ "$h" = "$PRIMARY_DB" ]; then
            echo "$h"
            return
        fi
    done
    for h in "$@"; do
        t=$(q "$h" "SELECT ('x' || substr(pg_walfile_name(pg_current_wal_lsn()), 1, 8))::bit(32)::bigint;")
        l=$(q "$h" "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint;")
        t="${t:-0}"; l="${l:-0}"
        if [ "$t" -gt "$best_tl" ] || { [ "$t" -eq "$best_tl" ] && [ "$l" -gt "$best_lsn" ]; }; then
            best="$h"; best_tl="$t"; best_lsn="$l"
        fi
    done
    echo "$best"
}
save_divergent() {
    local host="$1" f
    f="$STATE_DIR/divergent-${host}-$(date +%Y%m%d-%H%M%S).sql.gz"
    log "SAVE: dumping $host to $f before discarding its writes"
    timeout 300 pg_dump -w -h "$host" -U "$DB_USER" -d "$DB_NAME" 2>/dev/null | gzip > "$f"
    if [ "${PIPESTATUS[0]}" -ne 0 ]; then
        log "WARN: pg_dump of $host failed or timed out, continuing"
    fi
}
handle_split_brain() {
    local winner h rc=0
    log "CRIT: SPLIT-BRAIN - these nodes are all PRIMARY: $* ,CRIT: DO NOT write to more than one database."
    winner=$(pick_winner "$@")
    if [ -z "$winner" ]; then
        log "CRIT: could not pick a winner - refusing to guess, demote manually"
        return 1
    fi
    if [ "$winner" != "$PRIMARY_DB" ]; then
        log "WARN: last known primary was '${PRIMARY_DB:-<unknown>}', chose $winner by timeline/LSN"
        PRIMARY_DB="$winner"
        save_primary "$winner"
    fi
    log "CRIT: keeping $winner as PRIMARY, demoting the others (their unreplicated writes will be lost)"
    set_label "$winner" on
    for h in "$@"; do
        [ "$h" = "$winner" ] && continue
        set_label "$h" off
        if [ "$AUTO_FENCE" = "true" ]; then
            fence_node "$h"
        fi
        if [ "$SAVE_DIVERGENT" = "true" ]; then
            save_divergent "$h"
        fi
        if [ "$AUTO_DEMOTE" != "true" ]; then
            log "WARN: AUTO_DEMOTE=false, $h left running - demote it manually"
            rc=1
            continue
        fi
        demote_to_standby "$h" "$winner" || rc=1
    done
    return $rc
}
do_failover() {
    local old="$PRIMARY_DB" h lsn best="" best_lsn=-1
    for h in "${standbys[@]}"; do
        lsn=$(q "$h" "SELECT GREATEST(COALESCE(pg_wal_lsn_diff(pg_last_wal_receive_lsn(), '0/0'), 0), COALESCE(pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '0/0'), 0))::bigint;")
        lsn="${lsn:-0}"
        log "INFO: candidate $h received/replayed LSN offset $lsn"
        if [ "$lsn" -gt "$best_lsn" ]; then
            best="$h"; best_lsn="$lsn"
        fi
    done
    if [ -z "$best" ]; then
        log "CRIT: FAILOVER FAILED - no promotable standby"
        return 1
    fi
    log "FAILOVER: $old failed, no standby is receiving WAL, best candidate is $best"
    if [ "$AUTO_FENCE" = "true" ]; then
        fence_node "$old"
    fi
    log "FAILOVER: promoting $best..."
    if promote_node "$best"; then
        PRIMARY_DB="$best"
        save_primary "$PRIMARY_DB"
        set_sync_value "$PRIMARY_DB" ""
        for h in "${HOSTS[@]}"; do
            if [ "$h" = "$PRIMARY_DB" ]; then set_label "$h" on; else set_label "$h" off; fi
        done
        log "FAILOVER COMPLETE: $best = PRIMARY, $old = FAILED. Standbys will be repointed automatically."
        return 0
    fi
    log "CRIT: FAILOVER FAILED"
    return 1
}
check_rewind_prereqs() {
    local host="$1" wl dc su
    wl=$(q "$host" "SHOW wal_log_hints;")
    dc=$(q "$host" "SHOW data_checksums;")
    if [ "$wl" != "on" ] && [ "$dc" != "on" ]; then
        log "WARN: $host has neither wal_log_hints nor data_checksums enabled, pg_rewind will fail"
    fi
    su=$(q "$host" "SELECT rolsuper FROM pg_roles WHERE rolname = current_user;")
    if [ "$su" != "t" ]; then
        log "WARN: $DB_USER is not a superuser, ALTER SYSTEM / pg_promote() may fail unless explicitly granted"
    fi
}
FAILS=0
load_primary
log "PostgreSQL HA controller started"
log "  db  = $DB1_HOST (pod $DB1_POD)"
log "  db2 = $DB2_HOST (pod $DB2_POD)"
log "  db3 = $DB3_HOST (pod $DB3_POD)"
log "  known primary = ${PRIMARY_DB:-<unknown>}, interval = ${INTERVAL}s, threshold = ${FAIL_THRESHOLD}"
log "  auto promote = ${AUTO_PROMOTE}, fence = ${AUTO_FENCE}, demote = ${AUTO_DEMOTE}, repoint = ${REPOINT_STANDBYS}"
log "  sync repl = ${SYNC_REPL}"
log "  k8s labels = ${K8S_LABELS} (namespace ${K8S_NAMESPACE}, label ${LABEL_KEY}=true|false)"
while [ "$STOP" -eq 0 ]; do
    for h in "${HOSTS[@]}"; do
        reconcile_pod "$h"
    done
    refresh_roles
    primaries=(); standbys=()
    for h in "${HOSTS[@]}"; do
        case "${ROLE[$h]}" in
            primary) primaries+=("$h") ;;
            standby) standbys+=("$h") ;;
        esac
    done
    if [ "${#primaries[@]}" -gt 1 ]; then
        handle_split_brain "${primaries[@]}"
        nap "$INTERVAL"
        continue
    elif [ "${#primaries[@]}" -eq 1 ]; then
        if [ "$PRIMARY_DB" != "${primaries[0]}" ]; then
            if grep -qx "${primaries[0]}" "$FENCED_FILE" 2>/dev/null; then
                log "CRIT: ${primaries[0]} is primary but was fenced (stale data). Not adopting it. If you really want it: remove it from $FENCED_FILE and restart with PRIMARY_DB=${primaries[0]}"
                nap "$INTERVAL"
                continue
            fi
            log "INFO: primary is ${primaries[0]} (was '${PRIMARY_DB:-<unknown>}')"
            PRIMARY_DB="${primaries[0]}"
            save_primary "$PRIMARY_DB"
        fi
    elif [ -z "$PRIMARY_DB" ]; then
        log "WARN: no primary discovered yet (db=${ROLE[$DB1_HOST]}, db2=${ROLE[$DB2_HOST]}, db3=${ROLE[$DB3_HOST]}), waiting"
        nap "$INTERVAL"
        continue
    fi
    if [ "${#primaries[@]}" -eq 0 ]; then
        if [ "${ROLE[$PRIMARY_DB]:-down}" = "down" ] && [ "${#standbys[@]}" -ge 1 ]; then
            FAILS=$((FAILS + 1))
            log "FAIL: $PRIMARY_DB unreachable ($FAILS/$FAIL_THRESHOLD)"
            log "INFO: reachable standbys: ${standbys[*]}"
            if [ "$FAILS" -ge "$FAIL_THRESHOLD" ]; then
                if [ "$AUTO_PROMOTE" != "true" ]; then
                    log "WARN: AUTO_PROMOTE=false, standbys remain STANDBY"
                else
                    streaming_from=""
                    for h in "${standbys[@]}"; do
                        is_streaming "$h" && streaming_from="$streaming_from $h"
                    done
                    if [ -n "$streaming_from" ]; then
                        log "WARN:$streaming_from still streaming from $PRIMARY_DB - $PRIMARY_DB looks ALIVE"
                        log "WARN: refusing to promote (would cause split-brain)"
                    elif do_failover; then
                        FAILS=0
                    fi
                fi
            fi
        else
            FAILS=0
            log "WARN: no PRIMARY found (last known $PRIMARY_DB=${ROLE[$PRIMARY_DB]:-down}, standbys=${#standbys[@]})"
        fi
        nap "$INTERVAL"
        continue
    fi
    FAILS=0
    if [ "$REWIND_CHECKED" -eq 0 ]; then
        check_rewind_prereqs "$PRIMARY_DB"
        REWIND_CHECKED=1
    fi
    labels_reconcile
    fence_reconcile
    repoint_reconcile "$PRIMARY_DB"
    sync_reconcile "$PRIMARY_DB"
    nap "$INTERVAL"
done
log "INFO: controller stopped"
exit 0