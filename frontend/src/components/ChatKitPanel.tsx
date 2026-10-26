import { AssistantPanel, type AssistantPanelProps } from "@traccar/assistant-ui";
import { CHATKIT_API_URL, CHATKIT_API_DOMAIN_KEY } from "../lib/config";

type ChatKitPanelProps = Pick<AssistantPanelProps,
  "theme" | "onShowMap" | "onShowHtml" | "onClearPanel"
>;

export function ChatKitPanel(props: ChatKitPanelProps) {
  return (
    <div className="relative h-full w-full overflow-hidden border border-slate-200/60 bg-white shadow-card dark:border-slate-800/70 dark:bg-slate-900">
      <AssistantPanel
        apiUrl={CHATKIT_API_URL}
        domainKey={CHATKIT_API_DOMAIN_KEY}
        {...props}
      />
    </div>
  );
}
