import type { Metadata } from "next";
import Link from "next/link";

import { PageHeader } from "@/components/shell/page-header";
import { formatDateTimeIST } from "@/lib/format";
import { fetchConfig } from "@/lib/swing/fetch";
import { PAGES } from "@/lib/vocabulary";

import { SettingsForm } from "./_components/settings-form";
import { settingsSave } from "./actions";

/**
 * `/me/swing` — `docs/swing/05` §2's "/swing/settings (inside /me, not a hub tab)", SW14.
 *
 * The `sw_config` form: allocation capital, risk per trade under the server's ceiling, the
 * largest position, the most positions, the opening-range window, the stop mode and the three
 * liquidity floors. Two things are shown and are not controls, by design: the exposure rung,
 * which only the evening's settlement moves (`04` §8.4), and the execution flag, which only a
 * hand on the server flips (`02` Track C). Saving writes `sw_config_audit` on the way through.
 *
 * Under `/me` rather than the swing hub because it is about the person's money and limits, not
 * about today's tape — the same split that put profile and brokers here.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/me/swing"].title,
  description: PAGES["/me/swing"].blurb,
  robots: { index: false, follow: false },
};

const RUNGS = 4;

export default async function SwingSettingsPage() {
  const config = await fetchConfig();

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/me/swing"].title}
        blurb={PAGES["/me/swing"].blurb}
        meta={
          config ? (
            <span className="text-sm text-muted-foreground">
              Last changed {formatDateTimeIST(config.updated_at)}
              {config.updated_by ? ` by ${config.updated_by}` : ""}
            </span>
          ) : null
        }
      />

      {config ? (
        <>
          <section aria-label="Set by the system" className="space-y-2">
            <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
              Set by the system, shown here
            </h2>
            <dl className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-md border border-border/70 px-3 py-2">
                <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                  Exposure rung
                </dt>
                <dd className="text-sm" data-testid="rung">
                  Rung {config.exposure_level + 1} of {RUNGS}
                  <span className="block text-xs text-muted-foreground">
                    Moved by the evening from the last five closed trades. Not a control.
                  </span>
                </dd>
              </div>
              <div className="rounded-md border border-border/70 px-3 py-2">
                <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                  Execution
                </dt>
                <dd className="text-sm" data-testid="execution">
                  {config.execution_enabled
                    ? "Execution: enabled on this server"
                    : "Execution: disabled on this server"}
                  <span className="block text-xs text-muted-foreground">
                    Flipped by hand on the server, never from a page.
                  </span>
                </dd>
              </div>
              <div className="rounded-md border border-border/70 px-3 py-2">
                <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                  First live sessions
                </dt>
                <dd className="text-sm" data-testid="first-live">
                  {config.first_live_sessions_left > 0
                    ? `${config.first_live_sessions_left} left at half risk`
                    : "done"}
                  <span className="block text-xs text-muted-foreground">
                    Counted down by the evening once a live session closes.
                  </span>
                </dd>
              </div>
            </dl>
          </section>

          <section aria-label="Settings" className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
              Yours to set
            </h2>
            <SettingsForm action={settingsSave} config={config} />
          </section>

          <p className="max-w-[70ch] text-sm text-muted-foreground">
            Every save is written to the audit with who changed what. Nothing on this page can
            place an order, raise the rung or turn execution on; the ceilings are server
            configuration and a value above one is refused with the ceiling named. The plan built
            tonight uses what is saved here. See the tape on the{" "}
            <Link href={"/swing"} className="underline underline-offset-2">
              swing hub
            </Link>
            .
          </p>
        </>
      ) : (
        <p className="max-w-[70ch] text-sm text-muted-foreground" data-testid="not-seeded">
          The swing allocation is not set up on this deployment yet, or its settings could not be
          read just now — there is nothing to change until it is. The hub is at{" "}
          <Link href={"/swing"} className="underline underline-offset-2">
            /swing
          </Link>
          .
        </p>
      )}
    </div>
  );
}
