import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { ConsentBanner } from "@/components/consent/consent-banner";
import { CONSENT_COOKIE, parseConsent, serialiseConsent, ESSENTIAL_ONLY } from "@/lib/consent/state";

/**
 * Prompt 18 deliverable 5, as a visitor experiences it.
 *
 * The three properties that matter, and the one that is easy to break: the banner appears when no
 * choice has been made, both buttons are offered with no pre-selection, and pressing either writes
 * a cookie and takes the banner away. The easy-to-break one is that "Essential only" writes a
 * record with `analytics: false` — a copy-paste of the other handler would silently grant
 * everything to a visitor who explicitly refused it.
 */
function clearCookie() {
  document.cookie = `${CONSENT_COOKIE}=; Path=/; Max-Age=0`;
}

function storedConsent() {
  const raw = document.cookie
    .split("; ")
    .find((pair) => pair.startsWith(`${CONSENT_COOKIE}=`))
    ?.slice(CONSENT_COOKIE.length + 1);
  return parseConsent(raw);
}

afterEach(clearCookie);

describe("the consent banner", () => {
  it("appears when no choice has been made", () => {
    clearCookie();
    render(<ConsentBanner />);
    expect(screen.getByRole("region", { name: "Cookie choices" })).toBeInTheDocument();
  });

  it("offers both choices, with neither pre-selected", () => {
    clearCookie();
    render(<ConsentBanner />);
    expect(screen.getByRole("button", { name: "Essential only" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Allow all" })).toBeInTheDocument();
    expect(storedConsent()).toBeNull();
  });

  it("stays out of the way once a choice exists", () => {
    document.cookie = `${CONSENT_COOKIE}=${serialiseConsent(ESSENTIAL_ONLY)}; Path=/`;
    render(<ConsentBanner />);
    expect(screen.queryByRole("region", { name: "Cookie choices" })).not.toBeInTheDocument();
  });

  it("writes a refusal when essential-only is chosen", async () => {
    clearCookie();
    render(<ConsentBanner />);
    await userEvent.click(screen.getByRole("button", { name: "Essential only" }));

    const stored = storedConsent();
    expect(stored?.analytics).toBe(false);
    expect(stored?.preferences).toBe(false);
    expect(stored?.essential).toBe(true);
    expect(screen.queryByRole("region", { name: "Cookie choices" })).not.toBeInTheDocument();
  });

  it("writes a grant when allow-all is chosen", async () => {
    clearCookie();
    render(<ConsentBanner />);
    await userEvent.click(screen.getByRole("button", { name: "Allow all" }));

    const stored = storedConsent();
    expect(stored?.analytics).toBe(true);
    expect(stored?.preferences).toBe(true);
  });

  it("links to the privacy policy, where the choice can be revisited", () => {
    clearCookie();
    render(<ConsentBanner />);
    expect(screen.getByRole("link", { name: /privacy policy/i })).toHaveAttribute(
      "href",
      "/privacy-policy",
    );
  });

  it("does not trap the page behind a modal", () => {
    /* `role="region"`, not `role="dialog"`: nothing here is a decision a visitor must make before
       reading a legal page, and the DPDP Act asks for a real choice rather than a wall. */
    clearCookie();
    render(<ConsentBanner />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
