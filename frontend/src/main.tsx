import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

// Build stamp — check `window.__BUILD__` in the console to confirm the
// deployed bundle matches the latest commit.
(window as unknown as { __BUILD__: typeof __BUILD_INFO__ }).__BUILD__ = __BUILD_INFO__;
console.log(
  `%cchatkit-frontend ${__BUILD_INFO__.version} · ${__BUILD_INFO__.sha} · ${__BUILD_INFO__.builtAt}`,
  "font-weight:bold"
);

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element with id 'root' not found");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>
);
