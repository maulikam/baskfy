/**
 * The subjects the contact form offers.
 *
 * A second copy of `baskfy_api.schemas.SUPPORT_TOPICS`, kept in step by
 * `services/api/tests/test_support_topic_parity.py` — the same arrangement as the custom-filter
 * operand list (`lib/screens/operands.ts`). No `/meta/*` endpoint publishes it, and a select whose
 * options the API answers 400 for is worse than a duplicated array.
 *
 * The set is closed because the value reaches an email *subject line*, which is a header.
 */
export const SUPPORT_TOPICS = [
  "A number looks wrong",
  "Billing or invoices",
  "My account",
  "A feature request",
  "Something else",
] as const;

export type SupportTopic = (typeof SUPPORT_TOPICS)[number];
