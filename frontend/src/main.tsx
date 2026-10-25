import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

// Build stamp. The corner label and `window.__BUILD__` are how you confirm
// the page you opened is this build, not an older bundle.
(window as unknown as { __BUILD__: typeof __BUILD_INFO__ }).__BUILD__ = __BUILD_INFO__;
console.log(
  `%cchatkit-frontend ${__BUILD_INFO__.version} · ${__BUILD_INFO__.sha} · ${__BUILD_INFO__.builtAt}`,
  "font-weight:bold"
);

function buildStampLabel(info: typeof __BUILD_INFO__): string {
  const when = info.builtAt.match(/^\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})/);
  const built = when ? `${when[1]} ${when[2]}Z` : info.builtAt;
  return `v${info.version} · ${info.sha} · ${built}`;
}

function BuildStamp() {
  const label = buildStampLabel(__BUILD_INFO__);
  return (
    <div
      className="fixed right-2 top-2 z-50 rounded bg-white/85 px-1.5 py-0.5 font-mono text-[10px] leading-none text-slate-500 shadow-sm ring-1 ring-slate-200/80 dark:bg-slate-900/85 dark:text-slate-400 dark:ring-slate-700"
      title={`chatkit-frontend ${__BUILD_INFO__.version} · ${__BUILD_INFO__.sha} · ${__BUILD_INFO__.builtAt}`}
    >
      {label}
    </div>
  );
}

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element with id 'root' not found");
}

createRoot(container).render(
  <StrictMode>
    <App />
    <BuildStamp />
  </StrictMode>
);
