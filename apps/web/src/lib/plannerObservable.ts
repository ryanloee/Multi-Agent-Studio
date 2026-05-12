const OBSERVE_BLOCK_START = "```observe";

function normalizeTraceLines(body: string): string[] {
  return body
    .split("\n")
    .map((line) => line.trim())
    .map((line) => line.replace(/^[-*]\s*/, ""))
    .filter(Boolean);
}

export function parsePlannerObservableContent(text: string): {
  visibleContent: string;
  traceLines: string[];
} {
  const traceLines: string[] = [];
  let visibleContent = text;

  const blockPattern = /```observe\s*([\s\S]*?)```/gi;
  visibleContent = visibleContent.replace(blockPattern, (_match, body: string) => {
    traceLines.push(...normalizeTraceLines(body));
    return "";
  });

  const trailingStart = visibleContent.toLowerCase().lastIndexOf(OBSERVE_BLOCK_START);
  if (trailingStart >= 0) {
    const trailingBody = visibleContent.slice(trailingStart + OBSERVE_BLOCK_START.length);
    traceLines.push(...normalizeTraceLines(trailingBody));
    visibleContent = visibleContent.slice(0, trailingStart);
  }

  return {
    visibleContent: visibleContent.replace(/\n{3,}/g, "\n\n").trim(),
    traceLines,
  };
}
