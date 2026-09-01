#!/usr/bin/env bash
# G8 — the deploy runbook exists, matches the house format, and is honest about the blockers.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
DOC="$BLUE/docs/runbooks/07-deploy-phase-a.md"
[ -f "$DOC" ] || fail "no runbook at $DOC"

# Each of these is a step a person at 3am has to be able to find. The terraform pattern is a
# regex rather than the literal "terraform apply", because the runbook invokes it through the
# official Docker image (`hashicorp/terraform:1.9 apply`) — there is no terraform binary on the
# machines this was written from. The check is "does it say how to apply", not "does it contain a
# particular string".
grep -qiE 'terraform[^[:space:]]* apply' "$DOC" || fail "runbook never says how to apply the Terraform"
for phrase in "Symptom" "gate-password.sh" "alembic upgrade head" "rollback"; do
  grep -qi -- "$phrase" "$DOC" || fail "runbook never mentions '$phrase'"
done

# The honest part. The runbook must name what a person cannot do from this repository, or the
# first reader wastes an hour discovering it.
grep -qi "NEEDS-MAULIK" "$DOC" || fail "runbook does not point at the human-blocked list"
grep -qi "legal" "$DOC"        || fail "runbook does not mention the unreviewed legal drafts"

# And the blockers must actually be filed.
grep -q "AWS" "$ROOT/NEEDS-MAULIK.md" || fail "NEEDS-MAULIK.md has no AWS entry"

COUNT="$(ls "$BLUE/docs/runbooks/" | wc -l | tr -d ' ')"
echo "RUNBOOK OK — $DOC present, ${COUNT} runbooks in the set, blockers filed"
