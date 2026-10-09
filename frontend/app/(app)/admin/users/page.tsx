import { redirect } from "next/navigation";

import { UsersAdminClient } from "@/components/admin/UsersAdminClient";
import { getCurrentUser } from "@/lib/api-client";

// Forced dynamic: the role is checked against the live backend on every request.
export const dynamic = "force-dynamic";

export default async function UsersAdminPage() {
  const me = await getCurrentUser();
  // The API enforces admin-only access itself (403); this keeps non-admins off the page as well.
  if (me.role !== "admin") redirect("/chat");
  return (
    <div className="py-2">
      <UsersAdminClient currentUserId={me.id} />
    </div>
  );
}
