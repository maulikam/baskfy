import type { Metadata } from "next";

import { FnoRiskCaveat } from "@/components/options/caveats";
import { Disclosure } from "@/components/twt/disclosure";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatDateTimeIST, formatTradeDate } from "@/lib/format";
import { fetchOptionsConfig } from "@/lib/options/fetch";
import { GROUP_NAME } from "@/lib/options/view";
import { PAGES } from "@/lib/vocabulary";

import { saveOptionsConfig } from "./actions";
import { OptionsSettingsForm } from "./_components/settings-form";

/**
 * `/me/options` — `docs/options/05` §2: the options book's money settings with the server's
 * ceilings shown, a 422 rendered inline naming the ceiling, and **every `04` threshold read-only
 * with its doc anchor** (a threshold is changed by a written decision, not a form). The tab's
 * second allowed write; it moves no money — it cannot switch execution on, lift a pause or raise a
 * ceiling, none of which is a field.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/me/options"].title,
  description: PAGES["/me/options"].blurb,
};

function words(key: string): string {
  return key.replace(/_/g, " ");
}

export default async function OptionsSettingsPage() {
  const config = await fetchOptionsConfig();
  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/me/options"].title}
        blurb={PAGES["/me/options"].blurb}
      />
      <SectionTabs section="me" />
      <FnoRiskCaveat />

      {config === null ? (
        <p className="text-sm">
          The options service did not answer, so the settings cannot be shown.
        </p>
      ) : (
        <>
          <ul className="space-y-1 text-sm" data-testid="options-execution">
            {config.gates.map((gate) => (
              <li key={gate.group}>
                {GROUP_NAME[gate.group]}:{" "}
                {gate.execution_enabled
                  ? "execution is switched on on this server; the desk's own switches decide whether it is live"
                  : "execution disabled on this server — paper only"}
              </li>
            ))}
          </ul>

          {config.seeded ? (
            <OptionsSettingsForm action={saveOptionsConfig} config={config} />
          ) : (
            <p className="text-sm" data-testid="options-not-seeded">
              The options account has not been set up yet, so
              there is nothing to change here. It is set up once, by the
              operator, before the first paper session.
            </p>
          )}

          {config.book?.paused_until ? (
            <p className="text-sm text-warning">
              The book is paused until{" "}
              {formatTradeDate(config.book.paused_until)}. A pause is set by the
              loss limits, not by this form.
            </p>
          ) : null}

          <Disclosure
            summary="Every rule, read-only"
            count={config.thresholds.length}
            testId="options-thresholds"
          >
            <p>
              These are the strategy&rsquo;s rules. They change only by a
              written decision, never from a form.
            </p>
            <table className="w-full text-xs tabular-nums">
              <caption className="sr-only">
                The rules and where each is written down
              </caption>
              <thead className="text-left">
                <tr>
                  <th scope="col" className="py-1 pr-2 font-normal">
                    Section
                  </th>
                  <th scope="col" className="py-1 pr-2 font-normal">
                    Rule
                  </th>
                  <th scope="col" className="py-1 pr-2 font-normal">
                    Value
                  </th>
                  <th scope="col" className="py-1 font-normal">
                    Written down in
                  </th>
                </tr>
              </thead>
              <tbody>
                {config.thresholds.map((t) => (
                  <tr
                    key={`${t.section}.${t.key}`}
                    className="border-t border-border/40"
                  >
                    <td className="py-0.5 pr-2">{words(t.section)}</td>
                    <td className="pr-2">{words(t.key)}</td>
                    <td className="pr-2">{t.value}</td>
                    <td>{t.anchor.replace("04", "business rules")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Disclosure>

          {config.audit.length > 0 ? (
            <Disclosure
              summary="What changed, newest first"
              count={config.audit.length}
            >
              <ul className="space-y-0.5 text-xs">
                {config.audit.map((a) => (
                  <li key={`${a.changed_at}-${a.scope}-${a.key}`}>
                    {formatDateTimeIST(a.changed_at)} · {a.scope} ·{" "}
                    {words(a.key)}: {a.old_value ?? "unset"} →{" "}
                    {a.new_value ?? "unset"}
                  </li>
                ))}
              </ul>
            </Disclosure>
          ) : null}
        </>
      )}
    </div>
  );
}
