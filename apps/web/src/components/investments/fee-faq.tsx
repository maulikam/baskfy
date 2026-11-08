/**
 * Fee-math FAQ — docs/smallcase/04 §1. Accrued, not collected (this run).
 */

const FAQ: Array<{ q: string; a: string }> = [
  {
    q: "When is a platform fee accrued?",
    a: "On every buy and invest-more batch: base = min(₹100, 1.5% × amount), then 18% GST on the base. One ledger row per executed batch, with collected=false until Track B collection is enabled.",
  },
  {
    q: "What about SIPs?",
    a: "SIP instalments use a lower cap: base = min(₹10, 1.5% × amount) + 18% GST.",
  },
  {
    q: "Do rebalances and exits charge a platform fee?",
    a: "No. Rebalance, exit, partial exit, and customize accrue zero platform fee. Broker and statutory charges stay with the broker and are not modeled here.",
  },
  {
    q: "Are private (user-created) baskets free?",
    a: "No — they accrue the same buy / invest-more fees as published baskets, matching the observed product and keeping the ledger honest from day one.",
  },
  {
    q: "How are amounts rounded?",
    a: "Half-up to 2 decimals at write time, so the API, UI, and CSV never disagree on paisa.",
  },
];

export function FeeFaq() {
  return (
    <section aria-label="Fee FAQ" className="space-y-3">
      <h2 className="text-sm font-semibold">How fees work</h2>
      <ul className="space-y-3">
        {FAQ.map((item) => (
          <li
            key={item.q}
            className="rounded-xl border border-border/70 bg-card px-4 py-3 text-sm leading-relaxed"
          >
            <p className="font-medium text-foreground">{item.q}</p>
            <p className="mt-1.5 text-muted-foreground">{item.a}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
