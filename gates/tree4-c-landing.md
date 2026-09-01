# Gates: C — the public landing page

Scope: `/` shows "The sample screen could not be loaded" because `marketcap_cr` and `sharpe_12m`
need the `custom_columns` entitlement an anonymous visitor has not got. Decision recorded in
`PLAN-TREE4-INTEGRITY.md`: drop the two gated columns, do not move the paywall.

- [x] C1: The sample screen requests only columns the free tier may have.
  CHECK: cd decile-blueprint && uv run python -c "
from baskfy_core.seed_data import DEFAULT_COLUMNS
from baskfy_api.routers.screens import DEFAULT_RESULT_COLUMNS
import re,pathlib
src=pathlib.Path('apps/web/src/lib/marketing/sample-screen.ts').read_text()
m=re.search(r'SAMPLE_COLUMNS = \[(.*?)\]', src, re.S)
cols=[c.strip().strip('\"') for c in m.group(1).split(',') if c.strip()]
allowed=set(DEFAULT_COLUMNS)|set(DEFAULT_RESULT_COLUMNS)
print('GATED_IN_SAMPLE='+str([c for c in cols if c not in allowed]))"
  EXPECT: GATED_IN_SAMPLE=[]
  EVIDENCE: SAMPLE_COLUMNS=['close_raw','ret_12m','vol_12m'] · GATED_IN_SAMPLE=[] — measured against DEFAULT_COLUMNS + DEFAULT_RESULT_COLUMNS.

- [x] C2: The rendered landing page no longer carries the failure text.
  CHECK: bash gates/tree4-check-landing.sh 2>&1 | grep -E "^ERROR_TEXT|^LANDING"
  EXPECT: ERROR_TEXT=absent
  EVIDENCE: ERROR_TEXT=absent (was present before the change; the string is 'sample screen could not be loaded').

- [x] C3: The landing page actually shows ranked rows, not an empty table dressed as success.
  CHECK: bash gates/tree4-check-landing.sh 2>&1 | grep -E "^SAMPLE_ROWS"
  EXPECT: /SAMPLE_ROWS=[1-9]/
  EVIDENCE: SAMPLE_ROWS=9. Rendered cells read: 1 CUPID 284.03 +745.7% 58.0% · 2 HFCL 230.03 +223.6% 49.0% · 3 WELCORP 1,917.10 +117.3% 35.8%. Headers: Symbol, Close, 1Y return, Volatility 1Y.

- [x] C4: No paywall was moved — the entitlement code is untouched by this tree.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git diff --stat -- decile-blueprint/packages/core/src/baskfy_core/entitlements.py decile-blueprint/services/api/src/baskfy_api/entitlements.py decile-blueprint/services/api/src/baskfy_api/routers/screens.py | wc -l | awk '{print "ENTITLEMENT_FILES_CHANGED="$1}'
  EXPECT: ENTITLEMENT_FILES_CHANGED=0
  EVIDENCE: ENTITLEMENT_FILES_CHANGED=0 — entitlements.py (core and api) and routers/screens.py untouched by this tree.
