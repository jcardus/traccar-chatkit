import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

// Build stamp stays on `window.__BUILD__` and in the console so you can
// confirm which bundle loaded. It is not drawn on the map panel.
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
