#!/usr/bin/env bash
# apply_branch_restrictions.sh -- make the EDW repository's Bitbucket Cloud branch restrictions match
# bitbucket/branch-restrictions.json (default branch pattern: main).
#
# Idempotent: it lists the restrictions already on the pattern, then per kind CREATES a missing one, UPDATES one that
# differs (value, users or groups) and leaves an identical one alone. It never deletes; restrictions on the pattern
# that the file does not mention are reported and kept. Re-running it is always safe.
#
# Usage (a workspace or repository admin, deliberately, from a workstation or a one-off admin job):
#   export BITBUCKET_ADMIN_TOKEN=<token>              # read -s it from a vault; never pass it as an argument
#   export BITBUCKET_MERGE_GROUPS=edw-developers      # required: who may merge pull requests into main
#   export BITBUCKET_BREAK_GLASS_GROUPS=edw-platform-admins   # optional: who may push directly (unset = nobody)
#   bitbucket/apply_branch_restrictions.sh --workspace alfa --repo edw-flyway --dry-run   # review the payloads
#   bitbucket/apply_branch_restrictions.sh --workspace alfa --repo edw-flyway             # apply
#
# Options:
#   --workspace SLUG   Bitbucket workspace slug          (default: $BITBUCKET_WORKSPACE)
#   --repo SLUG        repository slug                   (default: $BITBUCKET_REPO)
#   --config FILE      desired state                     (default: branch-restrictions.json beside this script)
#   --pattern GLOB     branch glob to restrict           (default: the file's "pattern", i.e. main)
#   --skip KIND        leave this kind untouched; repeatable (e.g. enforce_merge_checks on a non-Premium plan)
#   --dry-run          print the payloads only: no API call, no token needed
#   -h, --help         this text
#
# Environment:
#   BITBUCKET_ADMIN_TOKEN   Bearer token with repository ADMIN scope: every branch-restrictions endpoint requires
#                           OAuth scope repository:admin / API-token scope admin:repository:bitbucket (swagger.json).
#                           A repository access token with "Repositories: Admin" works; the Jenkins
#                           bitbucket-status-token (read only) does not, and must not be widened for this.
#   BITBUCKET_MERGE_GROUPS, BITBUCKET_BREAK_GLASS_GROUPS
#                           comma-separated Bitbucket group SLUGS, read because branch-restrictions.json names them
#                           in "groups_env". An empty push list blocks direct pushes for everybody (API semantics).
#   BITBUCKET_WORKSPACE, BITBUCKET_REPO
#                           defaults for --workspace / --repo: the same names the Jenkins deploy jobs use. Not
#                           $WORKSPACE, which Jenkins sets to the build directory.
#
# Requires: bash 3.2+, curl 7.55+ (--header @file keeps the token out of the process list), jq 1.6+.
# Never run it with `bash -x`: tracing would print the token.
# Exit codes: 0 everything in place, 1 at least one API call failed, 2 usage or configuration error.
#
# Source: the branchrestriction schema and endpoint descriptions in api.bitbucket.org/swagger.json (read
# 2026-10-13): the kinds, which take a value, that only push / restrict_merges take users and groups, and that
# kind + glob pattern is unique per repository (so the kind identifies the restriction to update).
# Unverified, so check on first run: whether enforce_merge_checks needs Premium, and whether an empty push list also
# blocks "Merge via pull request". Run --dry-run, apply to a scratch repository first, then confirm in the UI
# (Repository settings > Branch restrictions).

set -euo pipefail

readonly API_ROOT='https://api.bitbucket.org/2.0'
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONFIG="$SCRIPT_DIR/branch-restrictions.json"
WS="${BITBUCKET_WORKSPACE:-}"
REPO="${BITBUCKET_REPO:-}"
PATTERN=''
DRY_RUN=false
SKIP_JSON='[]'
HEADER_FILE=''
RESP_FILE=''
BODY_FILE=''

# The kind enum of the current API, the kinds that carry an integer "value", and the only kinds that accept
# users/groups (everything else is a plain on/off switch).
readonly KNOWN_KINDS='["push","delete","force","restrict_merges","require_tasks_to_be_completed",
 "require_approvals_to_merge","require_review_group_approvals_to_merge","require_default_reviewer_approvals_to_merge",
 "require_no_changes_requested","require_passing_builds_to_merge","require_commits_behind",
 "reset_pullrequest_approvals_on_change","smart_reset_pullrequest_approvals",
 "reset_pullrequest_changes_requested_on_change","require_all_dependencies_merged","enforce_merge_checks",
 "allow_auto_merge_when_builds_pass","require_all_comments_resolved"]'
readonly VALUED_KINDS='["require_approvals_to_merge","require_default_reviewer_approvals_to_merge",
 "require_passing_builds_to_merge","require_commits_behind"]'
readonly MEMBER_KINDS='["push","restrict_merges"]'

cleanup() {
    local f
    for f in "$HEADER_FILE" "$RESP_FILE" "$BODY_FILE"; do
        if [[ -n $f ]]; then rm -f -- "$f"; fi
    done
}
trap cleanup EXIT

die() {  # die <exit code> <message>
    printf 'apply_branch_restrictions: %s\n' "$2" >&2
    exit "$1"
}

usage() {
    sed -n '2,/^# Exit codes/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

slug_ok() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; }

command -v jq >/dev/null 2>&1 || die 2 "jq is required"
command -v curl >/dev/null 2>&1 || die 2 "curl is required"

# ------------------------------------------------------------------ arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --workspace) [[ $# -ge 2 ]] || die 2 "--workspace needs a value"; WS=$2; shift 2 ;;
        --repo)      [[ $# -ge 2 ]] || die 2 "--repo needs a value"; REPO=$2; shift 2 ;;
        --config)    [[ $# -ge 2 ]] || die 2 "--config needs a value"; CONFIG=$2; shift 2 ;;
        --pattern)   [[ $# -ge 2 ]] || die 2 "--pattern needs a value"; PATTERN=$2; shift 2 ;;
        --skip)
            [[ $# -ge 2 ]] || die 2 "--skip needs a restriction kind"
            jq -e --arg k "$2" 'any(.[]; . == $k)' <<<"$KNOWN_KINDS" >/dev/null \
                || die 2 "--skip $2: not a branch-restriction kind"
            SKIP_JSON=$(jq -c --arg k "$2" '. + [$k] | unique' <<<"$SKIP_JSON")
            shift 2 ;;
        --dry-run)   DRY_RUN=true; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           die 2 "unknown argument '$1' (see --help)" ;;
    esac
done

[[ -f $CONFIG ]] || die 2 "config file not found: $CONFIG"
jq -e 'type == "object"' "$CONFIG" >/dev/null 2>&1 || die 2 "$CONFIG is not a JSON object"

# ------------------------------------------------------------------ validate the desired state
problems=$(jq -r --argjson known "$KNOWN_KINDS" --argjson valued "$VALUED_KINDS" --argjson members "$MEMBER_KINDS" '
    def has_kind($list): .kind as $k | $list | any(.[]; . == $k);
    (if (.restrictions | type) != "array" or (.restrictions | length) == 0
        then "\"restrictions\" must be a non-empty array" else empty end),
    (if (.branch_match_kind // "glob") != "glob"
        then "only branch_match_kind \"glob\" is supported" else empty end),
    (.restrictions[]? |
        (if has_kind($known) | not then "unknown kind \(.kind | tojson)" else empty end),
        (if has_kind($valued) and ((.value | type) != "number" or .value < 0 or .value != (.value | floor))
            then "\(.kind) needs an integer \"value\" >= 0" else empty end),
        (if (has_kind($valued) | not) and has("value") then "\(.kind) takes no \"value\"" else empty end),
        (if (has_kind($members) | not) and (has("users") or has("groups") or has("groups_env"))
            then "\(.kind) takes no users/groups (only push and restrict_merges do)" else empty end),
        (if has("users") and ([.users[]? | select((.uuid | type) != "string")] | length) > 0
            then "\(.kind): users are given as {\"uuid\": \"{...}\"}" else empty end)),
    ([.restrictions[]?.kind] | group_by(.)[] | select(length > 1) | "kind \(.[0]) is listed twice")
' "$CONFIG")
[[ -z $problems ]] || die 2 "invalid $CONFIG:"$'\n'"$problems"

if [[ -z $PATTERN ]]; then
    PATTERN=$(jq -r '.pattern // empty' "$CONFIG")
fi
[[ -n $PATTERN && $PATTERN != *[[:space:]]* ]] || die 2 "branch pattern is empty or contains whitespace"

# ------------------------------------------------------------------ group lists from the environment
group_list_json() {  # comma-separated slugs -> [{"slug": ...}, ...]
    local out='[]' slug
    local -a parts=()
    IFS=',' read -r -a parts <<<"$1"     # read -a: no pathname expansion of the values
    for slug in ${parts[@]+"${parts[@]}"}; do
        slug=${slug#"${slug%%[![:space:]]*}"}     # trim leading whitespace
        slug=${slug%"${slug##*[![:space:]]}"}     # trim trailing whitespace
        [[ -n $slug ]] || continue
        slug_ok "$slug" || die 2 "'$slug' is not a Bitbucket group slug"
        out=$(jq -c --arg s "$slug" '. + [{slug: $s}] | unique_by(.slug)' <<<"$out")
    done
    printf '%s' "$out"
}

ENV_GROUPS='{}'
while IFS= read -r name; do
    [[ -n $name ]] || continue
    [[ $name =~ ^[A-Z_][A-Z0-9_]*$ ]] || die 2 "groups_env '$name' is not an environment variable name"
    groups=$(group_list_json "${!name:-}")
    ENV_GROUPS=$(jq -c --arg k "$name" --argjson v "$groups" '. + {($k): $v}' <<<"$ENV_GROUPS")
done < <(jq -r '[.restrictions[] | .groups_env // empty] | unique[]' "$CONFIG")

# ------------------------------------------------------------------ the payloads to send (one JSON object per line)
PAYLOADS=$(jq -c --arg pattern "$PATTERN" --argjson eg "$ENV_GROUPS" --argjson skip "$SKIP_JSON" '
    .restrictions[]
    | select(.kind as $k | $skip | any(.[]; . == $k) | not)
    | {type: "branchrestriction", kind: .kind, branch_match_kind: "glob", pattern: $pattern}
      + (if has("value") then {value: .value} else {} end)
      + (if .kind == "push" or .kind == "restrict_merges"
           then {users: (.users // []),
                 groups: (((.groups // []) + (if .groups_env then $eg[.groups_env] else [] end))
                          | unique_by(.slug))}
           else {} end)
' "$CONFIG")
[[ -n $PAYLOADS ]] || die 2 "nothing to apply (every kind skipped?)"

# restrict_merges with nobody listed would stop EVERY merge into the pattern: refuse it.
if jq -e -s 'any(.[]; .kind == "restrict_merges" and (.users | length) == 0 and (.groups | length) == 0)' \
        <<<"$PAYLOADS" >/dev/null; then
    die 2 "restrict_merges has no users or groups: set BITBUCKET_MERGE_GROUPS (an empty list blocks every merge)"
fi

if $DRY_RUN; then
    printf '# dry run: %s/%s, branch pattern %s -- payloads below are what would be POSTed (new kind) or PUT\n' \
        "${WS:-<workspace>}" "${REPO:-<repo>}" "$PATTERN"
    printf '# (existing kind) to %s/repositories/<workspace>/<repo>/branch-restrictions; nothing was sent.\n' \
        "$API_ROOT"
    jq . <<<"$PAYLOADS"
    exit 0
fi

# ------------------------------------------------------------------ live run
[[ -n $WS ]] || die 2 "workspace missing: --workspace or BITBUCKET_WORKSPACE"
[[ -n $REPO ]] || die 2 "repository missing: --repo or BITBUCKET_REPO"
slug_ok "$WS" || die 2 "'$WS' is not a workspace slug"
slug_ok "$REPO" || die 2 "'$REPO' is not a repository slug"
[[ -n ${BITBUCKET_ADMIN_TOKEN:-} ]] || die 2 "BITBUCKET_ADMIN_TOKEN is not set"
[[ $BITBUCKET_ADMIN_TOKEN != *[[:space:]]* ]] || die 2 "BITBUCKET_ADMIN_TOKEN contains whitespace"

umask 077
HEADER_FILE=$(mktemp)
RESP_FILE=$(mktemp)
BODY_FILE=$(mktemp)
# printf is a shell builtin: the token never appears in a process listing, and curl reads it from this 0600 file.
printf 'Authorization: Bearer %s\n' "$BITBUCKET_ADMIN_TOKEN" >"$HEADER_FILE"

BASE_URL="$API_ROOT/repositories/$WS/$REPO/branch-restrictions"

api() {  # api METHOD URL [body file] -> prints the HTTP status; response body in $RESP_FILE
    local method=$1 url=$2 body=${3:-}
    local -a args=(--silent --show-error --max-time 60 --output "$RESP_FILE" --write-out '%{http_code}'
        --request "$method" --header "@$HEADER_FILE" --header 'Accept: application/json')
    if [[ $method == GET ]]; then
        args+=(--retry 3 --retry-delay 2)    # reads only: a retried write could double-apply
    fi
    if [[ -n $body ]]; then
        args+=(--header 'Content-Type: application/json' --data-binary "@$body")
    fi
    curl "${args[@]}" "$url"
}

api_error() {  # Bitbucket's error body: {"type": "error", "error": {"message": ..., "detail": ...}}
    jq -r '[.error.message, .error.detail] | map(select(. != null)) | join(": ")' "$RESP_FILE" 2>/dev/null \
        || head -c 300 "$RESP_FILE"
}

# Existing restrictions on the pattern (all pages). Only Bitbucket's own `next` links are followed, so the token
# is never sent anywhere else.
EXISTING='[]'
url="$BASE_URL?pattern=$(jq -rn --arg v "$PATTERN" '$v | @uri')&pagelen=100"
while [[ -n $url ]]; do
    [[ $url == "$API_ROOT/"* ]] || die 1 "refusing to follow a pagination link outside $API_ROOT: $url"
    code=$(api GET "$url") || die 1 "network error listing branch restrictions of $WS/$REPO"
    case "$code" in
        200) ;;
        401|403)
            die 1 "HTTP $code listing branch restrictions (the token needs repository ADMIN scope): $(api_error)" ;;
        404) die 1 "HTTP 404: repository $WS/$REPO not found or not visible to this token" ;;
        *) die 1 "HTTP $code listing branch restrictions: $(api_error)" ;;
    esac
    EXISTING=$(jq -c --argjson acc "$EXISTING" '$acc + (.values // [])' "$RESP_FILE")
    url=$(jq -r '.next // empty' "$RESP_FILE")
done
EXISTING=$(jq -c --arg p "$PATTERN" \
    '[.[] | select((.branch_match_kind // "glob") == "glob" and .pattern == $p)]' <<<"$EXISTING")

# Compare what matters; the API echoes extra fields (links, id, account details) that are not part of the policy.
readonly NORMALIZE='{kind, value: (.value // null),
    users: ([.users[]?.uuid] | sort), groups: ([.groups[]?.slug] | sort)}'

created=0 updated=0 unchanged=0 failed=0
while IFS= read -r payload; do
    kind=$(jq -r '.kind' <<<"$payload")
    current=$(jq -c --arg k "$kind" '[.[] | select(.kind == $k)] | first // empty' <<<"$EXISTING")
    printf '%s' "$payload" >"$BODY_FILE"
    if [[ -z $current ]]; then
        if ! code=$(api POST "$BASE_URL" "$BODY_FILE"); then
            printf 'FAILED  %-45s network error\n' "$kind" >&2; failed=$((failed + 1)); continue
        fi
        if [[ $code == 200 || $code == 201 ]]; then
            printf 'created %-45s id %s\n' "$kind" "$(jq -r '.id // "?"' "$RESP_FILE")"
            created=$((created + 1))
        else
            printf 'FAILED  %-45s POST HTTP %s: %s\n' "$kind" "$code" "$(api_error)" >&2
            failed=$((failed + 1))
        fi
        continue
    fi
    id=$(jq -r '.id' <<<"$current")
    have=$(jq -cS "$NORMALIZE" <<<"$current")
    want=$(jq -cS "$NORMALIZE" <<<"$payload")
    if [[ $have == "$want" ]]; then    # quoted: an unquoted right side is a glob pattern, and JSON has [ ]
        printf 'ok      %-45s id %s (unchanged)\n' "$kind" "$id"
        unchanged=$((unchanged + 1))
        continue
    fi
    [[ $id =~ ^[0-9]+$ ]] || die 1 "unexpected restriction id '$id' for $kind"
    if ! code=$(api PUT "$BASE_URL/$id" "$BODY_FILE"); then
        printf 'FAILED  %-45s network error\n' "$kind" >&2; failed=$((failed + 1)); continue
    fi
    if [[ $code == 200 ]]; then
        printf 'updated %-45s id %s\n' "$kind" "$id"
        updated=$((updated + 1))
    else
        printf 'FAILED  %-45s PUT HTTP %s: %s\n' "$kind" "$code" "$(api_error)" >&2
        failed=$((failed + 1))
    fi
done <<<"$PAYLOADS"

# Restrictions on the pattern that the file does not manage (and were not skipped): reported, never removed.
jq -r --argjson want "$(jq -s -c '[.[].kind]' <<<"$PAYLOADS")" --argjson skip "$SKIP_JSON" '
    .[] | select(.kind as $k | ($want + $skip) | any(.[]; . == $k) | not)
    | "kept    \(.kind) id \(.id) (not in the config; remove it in the Bitbucket UI if unwanted)"
' <<<"$EXISTING"

printf '%s/%s branch pattern %s: %d created, %d updated, %d unchanged, %d failed' \
    "$WS" "$REPO" "$PATTERN" "$created" "$updated" "$unchanged" "$failed"
if [[ $SKIP_JSON != '[]' ]]; then
    printf ', skipped %s' "$(jq -r 'join(", ")' <<<"$SKIP_JSON")"
fi
printf '\n'
[[ $failed -eq 0 ]] || exit 1
