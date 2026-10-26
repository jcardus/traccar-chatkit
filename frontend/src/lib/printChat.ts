// Builds a printable transcript from a ChatKit thread.
// threads.get_by_id returns only the first page (20 items). The rest comes
// from items.list. Message text is escaped before it is written into the
// print document.

const DEFAULT_PAGE_LIMIT = 100;
const DEFAULT_MAX_PAGES = 50;

export type PrintRole = "you" | "assistant" | "note";

export type PrintBlock = {
  role: PrintRole;
  text: string;
};

type Page = {
  data: unknown[];
  has_more: boolean;
  after: string | null;
};

export function postChatkit(apiUrl: string): (body: unknown) => Promise<unknown> {
  return async (body) => {
    const res = await fetch(apiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      throw new Error(`Chat request failed: ${res.status}`);
    }
    return (await res.json()) as unknown;
  };
}

export async function loadPrintableThread(
  threadId: string,
  post: (body: unknown) => Promise<unknown>,
  options?: { pageLimit?: number; maxPages?: number },
): Promise<{ title: string; blocks: PrintBlock[] }> {
  const pageLimit = options?.pageLimit ?? DEFAULT_PAGE_LIMIT;
  const maxPages = options?.maxPages ?? DEFAULT_MAX_PAGES;

  const thread = asRecord(
    await post({
      type: "threads.get_by_id",
      params: { thread_id: threadId },
    }),
  );
  const title = stringValue(thread?.title) || "Chat";
  const first = asPage(thread?.items);
  const items = [...first.data];

  let after = first.after;
  let hasMore = first.has_more;
  let pages = 0;
  while (hasMore && after && pages < maxPages) {
    pages += 1;
    const page = asPage(
      await post({
        type: "items.list",
        params: {
          thread_id: threadId,
          order: "asc",
          after,
          limit: pageLimit,
        },
      }),
    );
    items.push(...page.data);
    if (!page.has_more || !page.after || page.after === after) {
      break;
    }
    after = page.after;
    hasMore = page.has_more;
  }

  return { title, blocks: blocksFromItems(items) };
}

export function blocksFromItems(items: unknown[]): PrintBlock[] {
  const blocks: PrintBlock[] = [];
  for (const item of items) {
    const block = blockFromItem(item);
    if (block) blocks.push(block);
  }
  return blocks;
}

export function renderPrintDocument(options: {
  title: string;
  printedAt: Date;
  blocks: PrintBlock[];
}): string {
  const title = options.title.trim() || "Chat";
  const when = options.printedAt.toLocaleString();
  const messages = options.blocks
    .map((block) => {
      const label = block.role === "you" ? "You" : block.role === "assistant" ? "Assistant" : "Note";
      return `<article class="message ${block.role}"><h2>${label}</h2><div class="body">${renderMessageHtml(block.text)}</div></article>`;
    })
    .join("\n");

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${escapeHtml(title)}</title>
<style>
  @page { margin: 16mm; }
  html, body { margin: 0; background: #fff; color: #111; }
  body { font: 11pt/1.45 system-ui, sans-serif; }
  h1 { font-size: 18pt; line-height: 1.2; margin: 0 0 0.2rem; }
  .meta { margin: 0 0 1.4rem; color: #444; font-size: 9.5pt; }
  article { margin: 0 0 1rem; }
  h2 { margin: 0 0 0.15rem; font: 600 8.5pt/1.2 system-ui, sans-serif; letter-spacing: 0.04em; text-transform: uppercase; color: #444; }
  .body p { margin: 0 0 0.55rem; white-space: pre-wrap; }
  .note .body { font-style: italic; }
  pre { margin: 0 0 0.55rem; padding: 0.55rem 0.7rem; white-space: pre-wrap; background: #f3f4f6; font: 9.5pt/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.92em; }
  a { color: inherit; }
</style>
</head>
<body>
<h1>${escapeHtml(title)}</h1>
<p class="meta">Printed ${escapeHtml(when)}</p>
${messages}
</body>
</html>`;
}

// The frame is full-size so the browser lays the document out before printing.
// A zero-size frame prints a blank page in Chrome. Removed when the dialog closes.
export function printHtmlDocument(html: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const iframe = document.createElement("iframe");
    iframe.setAttribute("title", "Print conversation");
    iframe.style.cssText =
      "position:fixed;inset:0;width:100%;height:100%;border:0;background:#fff;z-index:2147483646";
    document.body.appendChild(iframe);

    const doc = iframe.contentDocument;
    const win = iframe.contentWindow;
    if (!doc || !win) {
      iframe.remove();
      reject(new Error("Could not open the print view."));
      return;
    }

    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      win.removeEventListener("afterprint", finish);
      iframe.remove();
      resolve();
    };

    try {
      doc.open();
      doc.write(html);
      doc.close();
    } catch (error) {
      iframe.remove();
      reject(error instanceof Error ? error : new Error("Could not open the print view."));
      return;
    }

    win.addEventListener("afterprint", finish);
    // If the dialog never reports afterprint, drop the frame so it cannot
    // cover the chat indefinitely. Long enough that a slow print is not cut off.
    window.setTimeout(finish, 3 * 60_000);
    win.requestAnimationFrame(() => {
      win.requestAnimationFrame(() => {
        try {
          win.focus();
          win.print();
        } catch (error) {
          if (settled) return;
          settled = true;
          win.removeEventListener("afterprint", finish);
          iframe.remove();
          reject(error instanceof Error ? error : new Error("Could not open the print view."));
        }
      });
    });
  });
}

function blockFromItem(item: unknown): PrintBlock | null {
  const record = asRecord(item);
  if (!record) return null;
  const type = stringValue(record.type);

  if (type === "user_message") {
    const parts: string[] = [];
    const quoted = stringValue(record.quoted_text);
    if (quoted) parts.push(quoted.split("\n").map((line) => `> ${line}`).join("\n"));
    const content = contentText(record.content);
    if (content) parts.push(content);
    for (const attachment of arrayValue(record.attachments)) {
      const file = asRecord(attachment);
      const name = stringValue(file?.name);
      if (name) parts.push(`Attachment: ${name}`);
    }
    const text = parts.join("\n\n").trim();
    return text ? { role: "you", text } : null;
  }

  if (type === "assistant_message") {
    const text = assistantText(record.content);
    return text ? { role: "assistant", text } : null;
  }

  if (type === "client_tool_call" && record.status === "completed") {
    if (record.name === "show_html") {
      return { role: "note", text: "Showed a report in the side panel." };
    }
    if (record.name === "show_map") {
      return { role: "note", text: "Showed a map." };
    }
    return null;
  }

  if (type === "widget") {
    const text = stringValue(record.copy_text);
    return text ? { role: "assistant", text } : null;
  }

  if (type === "structured_input") {
    const text = structuredInputText(record.inputs);
    return text ? { role: "note", text } : null;
  }

  return null;
}

function assistantText(content: unknown): string {
  const chunks: string[] = [];
  const sources: string[] = [];
  for (const part of arrayValue(content)) {
    const record = asRecord(part);
    if (!record) continue;
    const text = stringValue(record.text);
    if (text) chunks.push(text);
    for (const annotation of arrayValue(record.annotations)) {
      const label = sourceLabel(asRecord(annotation)?.source);
      if (label && !sources.includes(label)) sources.push(label);
    }
  }
  const body = chunks.join("\n\n").trim();
  const extra = sources.filter((label) => !body.includes(label));
  if (!extra.length) return body;
  const sourceBlock = `Sources:\n${extra.map((label) => `- ${label}`).join("\n")}`;
  return body ? `${body}\n\n${sourceBlock}` : sourceBlock;
}

function contentText(content: unknown): string {
  const chunks: string[] = [];
  for (const part of arrayValue(content)) {
    const record = asRecord(part);
    const text = stringValue(record?.text);
    if (text) chunks.push(text);
  }
  return chunks.join("\n").trim();
}

function structuredInputText(inputs: unknown): string {
  const lines: string[] = [];
  for (const input of arrayValue(inputs)) {
    const record = asRecord(input);
    if (!record) continue;
    const question = stringValue(record.question);
    if (question) lines.push(question);
    const answer = asRecord(record.answer);
    if (!answer) continue;
    if (answer.skipped === true) {
      lines.push("Skipped");
      continue;
    }
    const values = arrayValue(answer.values).filter((value): value is string => typeof value === "string" && value.trim() !== "");
    if (values.length) lines.push(values.join(", "));
  }
  return lines.join("\n").trim();
}

function sourceLabel(source: unknown): string | null {
  const src = asRecord(source);
  if (!src) return null;
  const title = stringValue(src.title);
  const url = stringValue(src.url);
  const filename = stringValue(src.filename);
  if (url && title) return `${title} (${url})`;
  if (url) return url;
  if (filename && title && filename !== title) return `${title} (${filename})`;
  return title || filename || null;
}

function renderMessageHtml(text: string): string {
  const lines = text.replaceAll("\r\n", "\n").split("\n");
  const parts: string[] = [];
  let paragraph: string[] = [];
  let code: string[] | null = null;

  const flushParagraph = () => {
    const raw = paragraph.join("\n").trim();
    paragraph = [];
    if (!raw) return;
    parts.push(`<p>${formatInline(raw)}</p>`);
  };

  for (const line of lines) {
    if (code) {
      if (line.startsWith("```")) {
        parts.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = null;
      } else {
        code.push(line);
      }
      continue;
    }
    if (line.startsWith("```")) {
      flushParagraph();
      code = [];
      continue;
    }
    if (line.trim() === "") {
      flushParagraph();
      continue;
    }
    paragraph.push(line);
  }
  if (code) {
    parts.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  }
  flushParagraph();
  return parts.join("\n");
}

function formatInline(raw: string): string {
  const escaped = escapeHtml(raw);
  const withCode = escaped.replace(/`([^`\n]+)`/g, (_match, code: string) => `<code>${code}</code>`);
  const withBold = withCode.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  return linkify(withBold);
}

function linkify(html: string): string {
  return html.replace(/https?:\/\/[^\s<>"']+/g, (match) => {
    const url = match.replace(/[.,);:]+$/g, "");
    const tail = match.slice(url.length);
    if (!url) return match;
    return `<a href="${url}">${url}</a>${tail}`;
  });
}

export function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function asPage(value: unknown): Page {
  const record = asRecord(value);
  const after = stringValue(record?.after);
  return {
    data: arrayValue(record?.data),
    has_more: record?.has_more === true,
    after,
  };
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringValue(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}
