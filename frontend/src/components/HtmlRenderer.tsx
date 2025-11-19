import { useEffect, useRef } from "react";

interface HtmlRendererProps {
  html: string | null;
  className?: string;
}

export default function HtmlRenderer({ html, className = "" }: HtmlRendererProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (containerRef.current && html) {
      // Clear previous content
      containerRef.current.innerHTML = "";

      // Create a wrapper div for the HTML content
      const wrapper = document.createElement("div");
      wrapper.innerHTML = html;

      containerRef.current.appendChild(wrapper);
    }
  }, [html]);

  if (!html) {
    return null;
  }

  return (
    <div
      ref={containerRef}
      className={`html-renderer overflow-auto ${className}`}
      style={{
        maxHeight: "100%",
        width: "100%",
      }}
    />
  );
}