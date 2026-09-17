import { ChatKit, useChatKit } from "@openai/chatkit-react";
import {
  CHATKIT_API_URL,
  CHATKIT_API_DOMAIN_KEY,
} from "../lib/config";
import type { ColorScheme } from "../hooks/useColorScheme";
import { useEffect, useRef } from "react";

type ChatKitPanelProps = {
  theme: ColorScheme;
  onShowMap: (invocation) => void;
  onShowHtml: (invocation) => void;
  onClearPanel: () => void;
};

type ThreadItem = {
  type: string;
  name?: string;
  status?: string;
  arguments?: { html_url?: string };
};

// Reconstructs the map/HTML panel when switching to a past (e.g. archived)
// thread. show_html client tool calls don't leave a visible thread item of
// their own, so nothing repopulates the panel by default when you reopen an
// old conversation -- this fetches the thread's items directly (the same
// threads.get_by_id op the widget itself uses to load history) and re-shows
// the last completed show_html call's HTML.
async function findLastShowHtml(threadId: string): Promise<string | null> {
  const res = await fetch(CHATKIT_API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      type: "threads.get_by_id",
      params: { thread_id: threadId },
    }),
  });
  if (!res.ok) {
    throw new Error(`threads.get_by_id failed: ${res.status}`);
  }
  const thread = await res.json();
  const items: ThreadItem[] = thread?.items?.data ?? [];
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (
      item.type === "client_tool_call" &&
      item.name === "show_html" &&
      item.status === "completed" &&
      item.arguments?.html_url
    ) {
      return item.arguments.html_url;
    }
  }
  return null;
}


export function ChatKitPanel({
  theme,
  onShowMap,
  onShowHtml,
  onClearPanel,
}: ChatKitPanelProps) {
  // Guards against a fast thread switch resolving out of order and
  // clobbering the panel with a stale response.
  const latestThreadRef = useRef<string | null>(null);

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
    },
    onThreadChange: (event: { threadId: string | null }) => {
      const threadId = event.threadId;
      latestThreadRef.current = threadId;
      if (!threadId) {
        onClearPanel();
        return;
      }
      findLastShowHtml(threadId)
        .then((htmlUrl) => {
          if (latestThreadRef.current !== threadId) return; // stale
          if (!htmlUrl) {
            onClearPanel();
            return;
          }
          return fetch(htmlUrl)
            .then((res) => (res.ok ? res.text() : null))
            .then((html) => {
              if (latestThreadRef.current !== threadId) return; // stale
              if (html) {
                onShowHtml({ params: { html } });
              } else {
                onClearPanel();
              }
            });
        })
        .catch((e) => {
          console.warn("Failed to restore panel for thread", threadId, e);
        });
    },
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
