import clsx from "clsx";
import { useCallback, useRef, useState } from "react";

import { ChatKitPanel } from "./ChatKitPanel";
import { ColorScheme } from "../hooks/useColorScheme";
import Map from "./Map";

const SPLIT_STORAGE_KEY = "chat-split-pct";
const MIN_PCT = 20;
const MAX_PCT = 85;
const DEFAULT_PCT = 66;

function readStoredSplit(): number {
  try {
    const raw = window.localStorage.getItem(SPLIT_STORAGE_KEY);
    const value = raw == null ? NaN : Number.parseFloat(raw);
    if (Number.isFinite(value) && value >= MIN_PCT && value <= MAX_PCT) {
      return value;
    }
  } catch {
    /* localStorage unavailable */
  }
  return DEFAULT_PCT;
}

export default function Home({
  scheme,
}: {
  scheme: ColorScheme;
}) {
  const [mapData, setMapData] = useState(null );
  const [showMap, setShowMap] = useState(true);
  const [showHtml, setShowHtml] = useState(true);
  const [htmlContent, setHtmlContent] = useState(null);

  const splitRef = useRef<HTMLDivElement>(null);
  const [leftPct, setLeftPct] = useState<number>(readStoredSplit);
  const [dragging, setDragging] = useState(false);

  const onSplitterMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const container = splitRef.current;
    if (!container || !event.currentTarget.hasPointerCapture(event.pointerId)) {
      return;
    }
    const rect = container.getBoundingClientRect();
    if (rect.width === 0) {
      return;
    }
    const pct = ((event.clientX - rect.left) / rect.width) * 100;
    setLeftPct(Math.min(MAX_PCT, Math.max(MIN_PCT, pct)));
  }, []);

  const onSplitterDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  }, []);

  const onSplitterUp = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
      setDragging(false);
      setLeftPct((pct) => {
        try {
          window.localStorage.setItem(SPLIT_STORAGE_KEY, String(Math.round(pct)));
        } catch {
          /* localStorage unavailable */
        }
        return pct;
      });
    },
    []
  );

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
        setHtmlContent(invocation.params.html);
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
        <div
          ref={splitRef}
          className={clsx(
            `relative w-full flex h-[calc(100vh)]
        items-stretch overflow-hidden
        bg-white/80 shadow-[0_45px_90px_-45px_rgba(15,23,42,0.6)] ring-1 ring-slate-200/60 backdrop-blur
         dark:bg-slate-900/70 dark:shadow-[0_45px_90px_-45px_rgba(15,23,42,0.85)] dark:ring-slate-800/60`,
            dragging && "cursor-col-resize select-none"
          )}
        >
          <div
            className="h-full min-w-0"
            style={{ width: `${leftPct}%`, pointerEvents: dragging ? "none" : undefined }}
          >
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
          <div
            role="separator"
            aria-orientation="vertical"
            onPointerDown={onSplitterDown}
            onPointerMove={onSplitterMove}
            onPointerUp={onSplitterUp}
            onPointerCancel={onSplitterUp}
            className="group relative z-10 flex w-px shrink-0 cursor-col-resize items-center justify-center bg-slate-200 transition-colors hover:bg-sky-400 dark:bg-slate-700 dark:hover:bg-sky-500"
          >
            {/* widen the pointer target without taking layout width */}
            <span className="absolute inset-y-0 -left-2 -right-2" />
            <span className="pointer-events-none h-10 w-1 rounded-full bg-slate-400/70 group-hover:bg-sky-400 dark:bg-slate-500/70 dark:group-hover:bg-sky-500" />
          </div>
          <div className="h-full min-w-0 flex-1">
            <ChatKitPanel
                theme={scheme}
                onShowMap={onShowMap}
                onShowHtml={onShowHtml}
            />
          </div>
        </div>
      </div>
    </div>
  )
}
