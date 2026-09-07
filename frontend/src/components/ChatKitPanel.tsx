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
        const screenshotUrl = invocation.params?.screenshot_url as
          | string
          | undefined;
        if (!screenshotUrl) {
          return { success: true };
        }
        // Warm the screenshot before answering the tool call. GET /chatkit/{png}
        // awaits the pending server-side screenshot task, so a 200 here means
        // the image is ready for the model to fetch. The value returned from
        // this callback is POSTed back as threads.add_client_tool_output, which
        // re-enters the server's respond() with the screenshot in hand. Only
        // hand back the URL if it actually resolved, so the model is never fed
        // a dead image link.
        let screenshotReady = false;
        for (let attempt = 0; attempt < 20; attempt++) {
          try {
            const res = await fetch(screenshotUrl, { cache: "no-store" });
            if (res.ok) {
              screenshotReady = true;
              break;
            }
          } catch (e) {
            console.warn("screenshot not ready, retrying", e);
          }
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
        return screenshotReady
          ? { success: true, screenshot_url: screenshotUrl }
          : { success: true };
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
        if (text) {
          trySend().then()
        } else {
          console.warn('ignoring', event.data)
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
