import { forwardRef } from "react";

interface HtmlRendererProps {
  html: string | null;
  className?: string;
}

const HtmlRenderer = forwardRef<HTMLIFrameElement, HtmlRendererProps>(
  ({ html, className = "" }, ref) => {
    if (!html) {
      return null;
    }

    return (
      <iframe
        ref={ref}
        srcDoc={html}
        className={className}
        style={{
          height: "100%",
          width: "100%",
          border: "none",
        }}
      />
    );
  }
);

HtmlRenderer.displayName = "HtmlRenderer";

export default HtmlRenderer;