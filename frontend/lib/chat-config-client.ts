import { authApiFetch } from "./api-client";
import { EMPTY_CHAT_CONFIG, mapChatConfig, type ChatConfig } from "./chat-models";

/** `GET /api/v1/chat/config`: what the chat composer offers (model picker, attachments). Never throws: on any
 * failure the composer simply offers neither. */
export async function getChatConfig(signal?: AbortSignal): Promise<ChatConfig> {
  try {
    return mapChatConfig(await authApiFetch<unknown>("/api/v1/chat/config", { signal }));
  } catch {
    return EMPTY_CHAT_CONFIG;
  }
}
