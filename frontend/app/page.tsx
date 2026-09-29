import { redirect } from "next/navigation";

/**
 * There is no separate Home page any more — Chat is the landing experience. Keep "/"
 * as a server-side redirect so old links/bookmarks to the root still land somewhere
 * useful.
 */
export default function RootPage() {
  redirect("/chat");
}
