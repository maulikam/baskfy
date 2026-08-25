# Gates: 1.2.2 SIP reminder persistence

Scope: POST creates a REMINDER sip plan on an investment; AUTO refused; Beat path still persists SIP_DUE.

- [x] G1: SIP API tests green
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_curated_sip_api.py --tb=line 2>&1 | tail -8
  EXPECT: passed
  EVIDENCE: ....                                                                     [100%] | 4 passed in 0.61s

- [x] G2: OpenAPI has POST /api/v1/cb/investments/{investment_id}/sip
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run python -c "from baskfy_api.app import create_app; p=create_app().openapi()['paths']; keys=[k for k in p if k.endswith('/sip')]; assert any('post' in p[k] for k in keys); print('SIP_OK', keys[0])"
  EXPECT: SIP_OK
  EVIDENCE: SIP_OK /api/v1/cb/investments/{investment_id}/sip

- [x] G3: sip-form testid exists
  CHECK: rg -n "sip-form" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/investments/sip-form.tsx
  EXPECT: sip-form
  EVIDENCE: 50:      data-testid="sip-form"
