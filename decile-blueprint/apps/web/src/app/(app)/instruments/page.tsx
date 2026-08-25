import { redirect } from "next/navigation";

/** `/instruments` has no catalog of its own — listings is the browse surface. */
export default function InstrumentsIndexPage() {
  redirect("/market/listings");
}
