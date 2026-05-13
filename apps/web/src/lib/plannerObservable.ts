const OBSERVE_BLOCK_START = "```observe";
const REPLY_BLOCK_START = "```reply";
const REPLY_BLOCK_PATTERN = /```reply\s*([\s\S]*?)```/i;
const HIDDEN_BLOCK_PATTERNS = [
  /```observe\s*([\s\S]*?)```/gi,
  /```reply\s*([\s\S]*?)```/gi,
  /```plan\s*([\s\S]*?)```/gi,
  /```action\s*([\s\S]*?)```/gi,
  /```ui-state\s*([\s\S]*?)```/gi,
  /```shared-doc\s*([\s\S]*?)```/gi,
];

const STRUCTURED_LEAK_MARKERS = [
  "下面是完整的 DAG",
  "以下是完整的 DAG",
  "完整的 DAG",
  "下面是完整工作流",
  "以下是完整工作流",
  "以下是为系统 Orchestrator",
  "为系统 Orchestrator 生成",
  "下面是生成的 DAG",
  "以下是生成的 DAG",
  "DAG：",
  "DAG:",
];

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
  const replyMatch = text.match(REPLY_BLOCK_PATTERN);
  let visibleContent = text;

  visibleContent = visibleContent.replace(/```observe\s*([\s\S]*?)```/gi, (_match, body: string) => {
    traceLines.push(...normalizeTraceLines(body));
    return "";
  });

  if (replyMatch?.[1]?.trim()) {
    return {
      visibleContent: replyMatch[1].trim(),
      traceLines,
    };
  }

  for (const pattern of HIDDEN_BLOCK_PATTERNS.slice(1)) {
    visibleContent = visibleContent.replace(pattern, "");
  }

  const trailingReplyStart = visibleContent.toLowerCase().lastIndexOf(REPLY_BLOCK_START);
  if (trailingReplyStart >= 0) {
    const replyBody = visibleContent
      .slice(trailingReplyStart + REPLY_BLOCK_START.length)
      .replace(/^\s*\n?/, "")
      .replace(/```[\s\S]*$/g, "")
      .trim();
    return {
      visibleContent: replyBody,
      traceLines,
    };
  }

  const trailingStart = visibleContent.toLowerCase().lastIndexOf(OBSERVE_BLOCK_START);
  if (trailingStart >= 0) {
    const trailingBody = visibleContent.slice(trailingStart + OBSERVE_BLOCK_START.length);
    traceLines.push(...normalizeTraceLines(trailingBody));
    visibleContent = visibleContent.slice(0, trailingStart);
  }

  for (const marker of STRUCTURED_LEAK_MARKERS) {
    const index = visibleContent.indexOf(marker);
    if (index >= 0) {
      visibleContent = visibleContent.slice(0, index);
      break;
    }
  }

  visibleContent = visibleContent
    .split("\n")
    .filter((line) => {
      const trimmed = line.trim();
      if (/^["']?(id|type|label|prompt|depends_on|nodes|edges|source|target)["']?\s*:/.test(trimmed)) {
        return false;
      }
      if (/^[}\]],?$/.test(trimmed)) return false;
      if (/^},?\s*$/.test(trimmed)) return false;
      return true;
    })
    .join("\n");

  return {
    visibleContent: visibleContent
      .replace(/\n{3,}/g, "\n\n")
      .replace(/^\s*```[\s\S]*$/gm, "")
      .trim(),
    traceLines,
  };
}
