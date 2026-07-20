#!/usr/bin/env bash
# Destination in your repo: .claude/hooks/check-okf-version.sh
# No chmod needed: settings.json invokes this via `bash`.
#
# SessionStart hook: compares the OKF version this repo declares in its stamp
# file (docs/index.md by default; relocatable via the layout block in
# docs/okf-map.yml, ADR 0018) against the latest spec version published on the
# OKF main branch, and the kit_version it declares against the kit's published
# VERSION file. When either differs, it injects a note into Claude's context;
# the "OKF version policy" and "Kit version policy" sections in the repo
# playbook tell the agent what to do. It also counts ADRs still proposed
# (frontmatter status:, a "- Status:" bullet, or a "# Status" section) so the
# owner's review inbox stays visible at session start (offline, local scan
# only).
# Fails silent (exit 0, no output) when offline, when the stamp file is absent
# or declares no okf_version / kit_version, or if upstream layouts change.
# Two further local-only notes: an installed map that still has no active
# mapping (check-stale inert, coarse docs/ gate active), and byte-identical
# kit hook copies outside .claude/hooks that no mirrors: declaration covers.

SPEC_URL="https://raw.githubusercontent.com/GoogleCloudPlatform/knowledge-catalog/main/okf/SPEC.md"
KIT_VERSION_URL="https://raw.githubusercontent.com/lilabrooks/claude-okf-repo-kit/main/VERSION"
ROOT="${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-.}}"

MAP_FILE=""
for map_candidate in "$ROOT/docs/okf-map.yml" "$ROOT/okf-map.yml"; do
  if [ -f "$map_candidate" ]; then
    MAP_FILE="$map_candidate"
    break
  fi
done

# Minimal copy of the scripts/okf layout parser (ADR 0018), kept inline so an
# unmodified copy of this hook still works when mirrored into another agent's
# config without scripts/okf beside it.
layout_value() {
  key="$1"
  default="$2"
  value=""
  if [ -n "$MAP_FILE" ]; then
    value="$(awk -v key="$key" '
      function clean(s) {
        gsub(/#.*/, "", s)
        gsub(/^[[:space:]"'\'']+/, "", s)
        gsub(/[[:space:]"'\'']+$/, "", s)
        return s
      }
      /^layout:/ { in_layout = 1; next }
      /^[^[:space:]#]/ { in_layout = 0 }
      in_layout {
        line = $0
        sub(/^[[:space:]]+/, "", line)
        if (index(line, key ":") == 1) {
          sub(key ":", "", line)
          print clean(line)
          exit
        }
      }
    ' "$MAP_FILE")"
  fi
  if [ -n "$value" ]; then
    printf '%s\n' "${value%/}"
  else
    printf '%s\n' "$default"
  fi
}

STAMP_REL="$(layout_value stamp_file docs/index.md)"
ADR_DIR_REL="$(layout_value adr_dir docs/adr)"
STAMP="$ROOT/$STAMP_REL"

notes=""

append_note() {
  if [ -n "$notes" ]; then
    notes="$notes $1"
  else
    notes="$1"
  fi
}

# No stamp file, or no okf_version declared in it, means the policy is
# inactive and the check stays silent — the same contract kit_version has
# always had. Brownfield repos that keep no bundle root are not nagged every
# session (ADR 0018). The network fetch is skipped entirely in that case.
declared=$(grep -m1 -oE 'okf_version:[[:space:]]*"?[0-9]+\.[0-9]+"?' "$STAMP" 2>/dev/null \
  | grep -oE '[0-9]+\.[0-9]+')

if [ -n "$declared" ]; then
  latest=$(curl -fsSL --max-time 5 "$SPEC_URL" 2>/dev/null \
    | grep -m1 -oE 'Version [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+')
  if [ -n "$latest" ] && [ "$latest" != "$declared" ]; then
    append_note "OKF version check: the latest OKF spec version on the official main branch is $latest. This repo's $STAMP_REL declares okf_version $declared. The OKF version policy in the repo playbook (CLAUDE.md; AGENTS.md if present) applies."
  fi
fi

kit_declared=$(grep -m1 -oE 'kit_version:[[:space:]]*"?[0-9]+\.[0-9]+\.[0-9]+"?' "$STAMP" 2>/dev/null \
  | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')

if [ -n "$kit_declared" ]; then
  kit_latest=$(curl -fsSL --max-time 5 "$KIT_VERSION_URL" 2>/dev/null \
    | grep -m1 -oE '[0-9]+\.[0-9]+\.[0-9]+')
  if [ -n "$kit_latest" ] && [ "$kit_latest" != "$kit_declared" ]; then
    append_note "Kit version check: this repo's $STAMP_REL says it was installed from claude-okf-repo-kit $kit_declared and the published kit version is $kit_latest. The Kit version policy in the repo playbook (CLAUDE.md; AGENTS.md if present) applies."
  fi
fi

# ADR review inbox: count ADRs whose status is `proposed`, skipping the index
# and installer-written numbered review candidates — the same files
# `scripts/okf pending` lists, with the same tolerant status detection
# (frontmatter `status:`, then a `- Status:` bullet, then a `# Status`
# section, normalized to the lowercased first word; ADR 0018).
pending_count=0
for adr in "$ROOT/$ADR_DIR_REL"/*.md; do
  [ -e "$adr" ] || continue
  case "$(basename "$adr")" in
    index.md|*.[0-9].md|*.[0-9][0-9].md) continue ;;
  esac
  status=$(grep -m1 -E '^status:' "$adr" 2>/dev/null | sed -E 's/^status:[[:space:]]*//; s/[[:space:]]+$//')
  if [ -z "$status" ]; then
    status=$(grep -m1 -E '^[-*][[:space:]]*[Ss]tatus:' "$adr" 2>/dev/null | sed -E 's/^[-*][[:space:]]*[Ss]tatus:[[:space:]]*//; s/[[:space:]]+$//')
  fi
  if [ -z "$status" ]; then
    status=$(awk 'found && NF { print; exit } /^#+[[:space:]]*[Ss]tatus[[:space:]]*$/ { found = 1 }' "$adr" 2>/dev/null)
  fi
  status=$(printf '%s' "$status" | awk '{ print tolower($1) }' | sed -E 's/[^a-z]+$//')
  [ "$status" = "proposed" ] && pending_count=$((pending_count + 1))
done

if [ "$pending_count" -gt 0 ]; then
  append_note "ADR review inbox: $pending_count ADR(s) are status: proposed awaiting the owner's review. List them with: bash scripts/okf pending. The decision policy in the repo playbook applies."
fi

# Kit candidate review: numbered review candidates (CLAUDE.2.md and friends)
# are inactive copies the installers wrote beside same-name files. The
# machine-local manifest records every one; a recorded candidate still on
# disk means the review-merge-delete pass hasn't happened. Stays silent when
# the manifest is absent (fresh clone, pre-manifest install). Local scan only.
CANDIDATE_MANIFEST="$ROOT/.okf-kit-backups/candidate-manifest"
if [ -f "$CANDIDATE_MANIFEST" ]; then
  unresolved=""
  unresolved_count=0
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    [ -f "$ROOT/$rel" ] || continue
    unresolved_count=$((unresolved_count + 1))
    if [ -n "$unresolved" ]; then unresolved="$unresolved, $rel"; else unresolved="$rel"; fi
  done < <(cut -f2 "$CANDIDATE_MANIFEST" | grep -E '\.[0-9]+(\.[A-Za-z0-9]+)?$' | sort -u)
  if [ "$unresolved_count" -gt 0 ]; then
    append_note "Kit candidate review: $unresolved_count unresolved numbered kit candidate file(s): $unresolved. These are inactive review copies written by the kit installer, not files any agent loads. Tell the owner: merge what they want into the live files, then delete the candidates - they should not be committed unresolved. The Kit version policy in the repo playbook applies."
  fi
fi

# Map coverage: a map with no active mapping means check-stale has nothing to
# guard yet and the Stop hook is still on its coarse docs/-prefix gate
# (ADR 0018). One line keeps the map's population visible until the first
# mapping lands. Local scan only; silent once a mapping exists and when no map
# file is present at all.
if [ -n "$MAP_FILE" ] && ! grep -qE '^[[:space:]]*-[[:space:]]*source:' "$MAP_FILE" 2>/dev/null; then
  MAP_REL="${MAP_FILE#"$ROOT"/}"
  append_note "OKF map check: $MAP_REL has no active mappings yet, so check-stale has nothing to guard and the Stop hook falls back to its coarse docs/ gate. Add a mapping when a source area gains its governing spec or ADR."
fi

# Undeclared mirror advisory (ADR 0021): a directory outside .claude/hooks
# holding a byte-identical copy of a kit hook is mirror-shaped, but the safe
# updater syncs only directories declared in the map's top-level mirrors:
# list. Detection here only ever warns — it never drives a sync — so a
# wrong-looking guess costs one advisory line, not an overwrite. Minimal copy
# of the verify-install mirrors parser, inline so a mirrored copy of this
# hook stands alone. Local scan only; silent when every mirror is declared.
declared_mirrors=""
if [ -n "$MAP_FILE" ]; then
  declared_mirrors="$(awk '
    function clean(s) {
      gsub(/#.*/, "", s)
      gsub(/^[[:space:]"'\'']+/, "", s)
      gsub(/[[:space:]"'\''\/]+$/, "", s)
      return s
    }
    /^mirrors:/ { in_mirrors = 1; next }
    /^[^[:space:]#]/ { in_mirrors = 0 }
    in_mirrors {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      if (line ~ /^-/) {
        sub(/^-[[:space:]]*/, "", line)
        line = clean(line)
        if (line != "") print line
      }
    }
  ' "$MAP_FILE")"
fi

undeclared_mirrors=""
if [ -d "$ROOT/.claude/hooks" ]; then
  while IFS= read -r found; do
    [ -n "$found" ] || continue
    rel="${found#"$ROOT"/}"
    case "$rel" in */*) ;; *) continue ;; esac
    dir="${rel%/*}"
    hook_name="${rel##*/}"
    [ "$dir" = ".claude/hooks" ] && continue
    printf '%s\n' "$declared_mirrors" | grep -qxF "$dir" && continue
    [ -f "$ROOT/.claude/hooks/$hook_name" ] || continue
    cmp -s "$ROOT/.claude/hooks/$hook_name" "$found" || continue
    case " $undeclared_mirrors " in
      *" $dir "*) ;;
      *) undeclared_mirrors="${undeclared_mirrors:+$undeclared_mirrors }$dir" ;;
    esac
  done < <(find "$ROOT" -maxdepth 4 \( -name .git -o -name .okf-kit-backups -o -name node_modules \) -prune -o -type f \( -name check-docs-sync.sh -o -name check-okf-version.sh \) -print 2>/dev/null)
fi

if [ -n "$undeclared_mirrors" ]; then
  append_note "Second-agent mirror check: undeclared kit hook mirror(s) at $undeclared_mirrors - byte-identical to the .claude/hooks originals but absent from the top-level mirrors: list in docs/okf-map.yml, so the safe updater will not sync them on kit upgrades. Recommend declaring them to the owner; the okf-second-agent skill carries the full port procedure."
fi

if [ -n "$notes" ]; then
  cat <<EOF
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "$notes"}}
EOF
fi

exit 0
