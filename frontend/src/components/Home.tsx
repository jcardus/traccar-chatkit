import clsx from "clsx";
import { useState, useRef } from "react";

import { ChatKitPanel } from "./ChatKitPanel";
import { ThemeToggle } from "./ThemeToggle";
import { ColorScheme } from "../hooks/useColorScheme";
import Map from "./Map";
import HtmlRenderer from "./HtmlRenderer";

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
  const htmlRendererRef = useRef<HTMLIFrameElement>(null);

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

  const handlePrint = () => {
    if (htmlRendererRef.current?.contentWindow) {
      htmlRendererRef.current.contentWindow.print();
    }
  }

  return (
    <div className={containerClass}>
      <div className="mx-auto flex min-h-screen w-full max-w-12xl flex-col-reverse px-4 pt-4 pb-10 md:py-4 lg:flex-row">
        <div className="
        relative w-full flex h-[calc(100vh-32px)]
        items-stretch overflow-hidden rounded-3xl
        bg-white/80 shadow-[0_45px_90px_-45px_rgba(15,23,42,0.6)] ring-1 ring-slate-200/60 backdrop-blur
         dark:bg-slate-900/70 dark:shadow-[0_45px_90px_-45px_rgba(15,23,42,0.85)] dark:ring-slate-800/60">
          <div className="absolute z-40 p-3 flex gap-2">
            <ThemeToggle value={scheme} onChange={handleThemeChange}  />
            <button
              onClick={() => setShowMap(!showMap)}
              className="rounded-lg p-2 transition-colors hover:bg-slate-200/60 dark:hover:bg-slate-800/60"
              title={showMap ? "Hide Map" : "Show Map"}
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={1.5}
                stroke="currentColor"
                className="h-5 w-5"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d={showMap ? "M3.98 8.223A10.477 10.477 0 001.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.45 10.45 0 0112 4.5c4.756 0 8.773 3.162 10.065 7.498a10.523 10.523 0 01-4.293 5.774M6.228 6.228L3 3m3.228 3.228l3.65 3.65m7.894 7.894L21 21m-3.228-3.228l-3.65-3.65m0 0a3 3 0 10-4.243-4.243m4.242 4.242L9.88 9.88" : "M9 6.75V15m6-6v8.25m.503 3.498l4.875-2.437c.381-.19.622-.58.622-1.006V4.82c0-.836-.88-1.38-1.628-1.006l-3.869 1.934c-.317.159-.69.159-1.006 0L9.503 3.252a1.125 1.125 0 00-1.006 0L3.622 5.689C3.24 5.88 3 6.27 3 6.695V19.18c0 .836.88 1.38 1.628 1.006l3.869-1.934c.317-.159.69-.159 1.006 0l4.994 2.497c.317.158.69.158 1.006 0z"}
                />
              </svg>
            </button>
            {showHtml && htmlContent && (
              <button
                onClick={handlePrint}
                className="rounded-lg p-2 transition-colors hover:bg-slate-200/60 dark:hover:bg-slate-800/60"
                title="Print HTML Content"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={1.5}
                  stroke="currentColor"
                  className="h-5 w-5"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M6.72 13.829c-.24.03-.48.062-.72.096m.72-.096a42.415 42.415 0 0110.56 0m-10.56 0L6.34 18m10.94-4.171c.24.03.48.062.72.096m-.72-.096L17.66 18m0 0l.229 2.523a1.125 1.125 0 01-1.12 1.227H7.231c-.662 0-1.18-.568-1.12-1.227L6.34 18m11.318 0h1.091A2.25 2.25 0 0021 15.75V9.456c0-1.081-.768-2.015-1.837-2.175a48.055 48.055 0 00-1.913-.247M6.34 18H5.25A2.25 2.25 0 013 15.75V9.456c0-1.081.768-2.015 1.837-2.175a48.041 48.041 0 011.913-.247m10.5 0a48.536 48.536 0 00-10.5 0m10.5 0V3.375c0-.621-.504-1.125-1.125-1.125h-8.25c-.621 0-1.125.504-1.125 1.125v3.659M18 10.5h.008v.008H18V10.5zm-3 0h.008v.008H15V10.5z"
                  />
                </svg>
              </button>
            )}
          </div>
          <ChatKitPanel
              theme={scheme}
              onShowMap={onShowMap}
              onShowHtml={onShowHtml}
          />
          {showMap && <Map data={mapData}></Map>}
          {showHtml && htmlContent && (
            <div className="w-full h-full p-0 m-0 bg-white">
              <HtmlRenderer ref={htmlRendererRef} html={htmlContent} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
