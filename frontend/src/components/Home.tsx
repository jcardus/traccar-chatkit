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

  const containerClass = clsx(
    "min-h-screen bg-gradient-to-br transition-colors duration-300",
    scheme === "dark"
      ? "from-slate-900 via-slate-950 to-slate-850 text-slate-100"
      : "from-slate-100 via-white to-slate-200 text-slate-900"
  );

  const onShowMap = (invocation: { params: { geojson: string; }; }) => {
    if (invocation?.params?.geojson) {
        const geojsonData = typeof invocation.params.geojson === 'string'
          ? JSON.parse(invocation.params.geojson)
          : invocation.params.geojson;
        setMapData(geojsonData);
    }
  }

  return (
    <div className={containerClass}>
      <div className="mx-auto flex min-h-screen w-full max-w-12xl flex-col-reverse px-6 pt-4 pb-10 md:py-4 lg:flex-row">
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
          </div>
          <ChatKitPanel
              theme={scheme}
              onShowMap={onShowMap}
          />
          {showMap && <Map data={mapData}></Map>}
        </div>
      </div>
    </div>
  )
}
