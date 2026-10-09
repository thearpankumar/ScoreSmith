import { notFound } from "next/navigation";

import { SharedChatView } from "@/components/chat/SharedChatView";
import { getSharedChat } from "@/lib/chat-share-client";

// Forced dynamic: live backend fetch on every request.
export const dynamic = "force-dynamic";

export default async function SharedChatPage({ params }: { params: Promise<{ sessionId: string }> }) {
  const { sessionId } = await params;
  const chat = await getSharedChat(sessionId);
  if (!chat) notFound();
  return <SharedChatView chat={chat} />;
}
