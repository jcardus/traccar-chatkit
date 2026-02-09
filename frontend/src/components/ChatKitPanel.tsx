import { ChatKit, useChatKit } from "@openai/chatkit-react";
import {
  CHATKIT_API_URL,
  CHATKIT_API_DOMAIN_KEY,
} from "../lib/config";
import type { ColorScheme } from "../hooks/useColorScheme";
import { useEffect } from "react";

type ChatKitPanelProps = {
  theme: ColorScheme;
  onShowMap: (invocation) => void;
  onShowHtml: (invocation) => void;
};

export function ChatKitPanel({
  theme,
  onShowMap,
  onShowHtml
}: ChatKitPanelProps) {

  const chatkit = useChatKit({
    api: { url: CHATKIT_API_URL, domainKey: CHATKIT_API_DOMAIN_KEY },
    theme: {
      colorScheme: theme,
      color: {
        grayscale: {
          hue: 220,
          tint: 6,
          shade: theme === "dark" ? -1 : -4,
        },
        accent: {
          primary: theme === "dark" ? "#f1f5f9" : "#0f172a",
          level: 1,
        },
      },
      radius: "round",
    },
    threadItemActions: {
      feedback: false,
    },
    onClientTool: async (invocation) => {
      if (invocation.name === "show_map") {
        console.log("show_map", invocation);
        onShowMap(invocation);
        return { success: true };
      } else if (invocation.name === "show_html") {
        onShowHtml(invocation);
        let attachment;
        try {
          const raw = invocation.params?.attachment;
          attachment = typeof raw === "string" ? JSON.parse(raw) : raw;
        } catch (e) {
          console.error("Failed to parse attachment:", e);
        }
        if (attachment) {
          const send = async () => {
            try {
              await chatkit.sendUserMessage({
                attachments: [
                  {
                    id: attachment.id,
                    type: attachment.type,
                    name: attachment.name,
                    mime_type: attachment.mime_type,
                    preview_url: attachment.preview_url,
                  },
                ],
              });
            } catch (e) {
              console.error("Failed to send screenshot message:", e);
              setTimeout(send, 5000);
            }
          };
          send();
        }
        return { success: true };
      }
      return { success: false };
    }
  });

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (event.data?.type === "html-error") {
        const text = event.data.message
        const trySend = async () => {
          try {
            await chatkit.sendUserMessage({text})
          } catch (e) {
            console.error(e)
            setTimeout(trySend, 500)
          }
        }
      }
    };
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [chatkit]);

  return (
    <div className="relative h-full w-full overflow-hidden border border-slate-200/60 bg-white shadow-card dark:border-slate-800/70 dark:bg-slate-900">
      <ChatKit control={chatkit.control} className="block h-full w-full"  />
    </div>
  );
}
