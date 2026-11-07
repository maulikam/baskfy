import { redirect } from "next/navigation";

/* Today's headline strip and index table now live on Indices. */
export default function MarketTodayPage() {
  redirect("/market/indices");
}
