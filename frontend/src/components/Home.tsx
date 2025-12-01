import clsx from "clsx";
import { useState } from "react";

import { ChatKitPanel } from "./ChatKitPanel";
import { ThemeToggle } from "./ThemeToggle";
import { ColorScheme } from "../hooks/useColorScheme";
import Map from "./Map";

export default function Home({
  scheme,
  handleThemeChange,
}: {
  scheme: ColorScheme;
  handleThemeChange: (scheme: ColorScheme) => void;
}) {
  const [mapData, setMapData] = useState(null );
  const [showMap, setShowMap] = useState(true);
  const [showHtml, setShowHtml] = useState(true);
  const [htmlContent, setHtmlContent] = useState(null);

  const containerClass = clsx(
    "min-h-screen bg-gradient-to-br transition-colors duration-300",
    scheme === "dark"
      ? "from-slate-900 via-slate-950 to-slate-850 text-slate-100"
      : "from-slate-100 via-white to-slate-200 text-slate-900"
  );

  const onShowHtml = (invocation: { params: { html: string; }; }) => {
    if (invocation?.params?.html) {
        setShowMap(false);
        setShowHtml(true);
        setHtmlContent(invocation?.params?.html);
    }
  }

  const onShowMap = (invocation: { params: { geojson: string; }; }) => {
    if (invocation?.params?.geojson) {
        const geojsonData = typeof invocation.params.geojson === 'string'
          ? JSON.parse(invocation.params.geojson)
          : invocation.params.geojson;
        setMapData(geojsonData);
        setShowMap(true);
        setShowHtml(false);
    }
  }

  return (
    <div className={containerClass}>
      <div className="mx-auto flex min-h-screen w-full max-w-12xl flex-col-reverse lg:flex-row">
        <div className="
        relative w-full flex h-[calc(100vh)]
        items-stretch overflow-hidden
        bg-white/80 shadow-[0_45px_90px_-45px_rgba(15,23,42,0.6)] ring-1 ring-slate-200/60 backdrop-blur
         dark:bg-slate-900/70 dark:shadow-[0_45px_90px_-45px_rgba(15,23,42,0.85)] dark:ring-slate-800/60">
          <div className="absolute z-40 p-3 flex gap-2">
            <ThemeToggle value={scheme} onChange={handleThemeChange}  />
          </div>
          <ChatKitPanel
              theme={scheme}
              onShowMap={onShowMap}
              onShowHtml={onShowHtml}
          />
          {showMap && <Map data={mapData}></Map>}
          {showHtml && htmlContent && (
            <div className="w-full h-full p-0 m-0 bg-white">
                <iframe
                    srcDoc={htmlContent}
                    style={{
                        height: "100%",
                        width: "100%",
                        border: "none",
                    }}
                />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
