"use client";

import type { ReactNode } from "react";

interface MarkdownMessageProps {
  content: string;
  compact?: boolean;
  inverted?: boolean;
}

type Block =
  | { type: "code"; language: string; content: string }
  | { type: "table"; rows: string[][] }
  | { type: "list"; ordered: boolean; items: string[] }
  | { type: "paragraph"; lines: string[] };

function parseBlocks(content: string): Block[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }

    const fence = line.match(/^```(\w+)?\s*$/);
    if (fence) {
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      blocks.push({ type: "code", language: fence[1] || "", content: codeLines.join("\n") });
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i + 1])) {
      const rows: string[][] = [splitTableRow(line)];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitTableRow(lines[i]));
        i += 1;
      }
      blocks.push({ type: "table", rows });
      continue;
    }

    const listMatch = line.match(/^(\s*)([-*+]|\d+\.)\s+(.+)$/);
    if (listMatch) {
      const ordered = /\d+\./.test(listMatch[2]);
      const items: string[] = [];
      while (i < lines.length) {
        const match = lines[i].match(/^(\s*)([-*+]|\d+\.)\s+(.+)$/);
        if (!match || /\d+\./.test(match[2]) !== ordered) break;
        items.push(match[3]);
        i += 1;
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    const paragraph: string[] = [];
    while (i < lines.length && lines[i].trim()) {
      if (lines[i].match(/^```/) || lines[i].match(/^(\s*)([-*+]|\d+\.)\s+(.+)$/)) break;
      if (lines[i].includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i + 1])) break;
      paragraph.push(lines[i]);
      i += 1;
    }
    blocks.push({ type: "paragraph", lines: paragraph });
  }

  return blocks;
}

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function renderInline(text: string, inverted: boolean): ReactNode[] {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g);
  return parts.map((part, idx) => {
    if (part.startsWith("`") && part.endsWith("`")) {
      return (
        <code
          key={idx}
          className={`rounded px-1 py-0.5 font-mono text-[0.92em] ${
            inverted ? "bg-white/15 text-white" : "bg-gray-100 text-gray-800"
          }`}
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={idx} className="font-semibold">{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("*") && part.endsWith("*")) {
      return <em key={idx} className="italic">{part.slice(1, -1)}</em>;
    }
    const linkMatch = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (linkMatch) {
      return (
        <a
          key={idx}
          href={linkMatch[2]}
          target="_blank"
          rel="noreferrer"
          className={`underline underline-offset-2 ${inverted ? "text-white" : "text-blue-600"}`}
        >
          {linkMatch[1]}
        </a>
      );
    }
    return part;
  });
}

function renderParagraph(lines: string[], compact: boolean, inverted: boolean): ReactNode {
  return lines.map((line, idx) => {
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const size = compact
        ? level <= 2 ? "text-[13px]" : "text-xs"
        : level <= 2 ? "text-base" : "text-sm";
      return (
        <div key={idx} className={`${idx > 0 ? "mt-2" : ""} ${size} font-semibold ${inverted ? "text-white" : "text-gray-900"}`}>
          {renderInline(heading[2], inverted)}
        </div>
      );
    }

    if (line.startsWith(">")) {
      return (
        <blockquote
          key={idx}
          className={`border-l-2 pl-2 italic ${inverted ? "border-white/40 text-white/85" : "border-gray-300 text-gray-600"}`}
        >
          {renderInline(line.replace(/^>\s?/, ""), inverted)}
        </blockquote>
      );
    }

    return (
      <div key={idx} className={idx > 0 ? "mt-1" : ""}>
        {renderInline(line, inverted)}
      </div>
    );
  });
}

export default function MarkdownMessage({ content, compact = false, inverted = false }: MarkdownMessageProps) {
  const blocks = parseBlocks(content);
  const textClass = inverted ? "text-white" : "text-gray-700";
  const gapClass = compact ? "space-y-1.5" : "space-y-3";

  return (
    <div className={`${gapClass} ${textClass} break-words`}>
      {blocks.map((block, idx) => {
        if (block.type === "code") {
          return (
            <pre
              key={idx}
              className={`overflow-x-auto rounded-md px-3 py-2 font-mono text-[11px] leading-relaxed ${
                inverted ? "bg-black/20 text-white" : "bg-gray-900 text-gray-50"
              }`}
            >
              <code>{block.content}</code>
            </pre>
          );
        }

        if (block.type === "table") {
          const [header, ...body] = block.rows;
          return (
            <div key={idx} className="overflow-x-auto">
              <table className={`min-w-full border-collapse text-left ${compact ? "text-[11px]" : "text-xs"}`}>
                <thead>
                  <tr>
                    {header.map((cell, cellIdx) => (
                      <th key={cellIdx} className={`border px-2 py-1 font-semibold ${inverted ? "border-white/20" : "border-gray-200 bg-gray-50"}`}>
                        {renderInline(cell, inverted)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {body.map((row, rowIdx) => (
                    <tr key={rowIdx}>
                      {row.map((cell, cellIdx) => (
                        <td key={cellIdx} className={`border px-2 py-1 align-top ${inverted ? "border-white/20" : "border-gray-200"}`}>
                          {renderInline(cell, inverted)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }

        if (block.type === "list") {
          const ListTag = block.ordered ? "ol" : "ul";
          return (
            <ListTag key={idx} className={`${block.ordered ? "list-decimal" : "list-disc"} pl-5 space-y-1`}>
              {block.items.map((item, itemIdx) => (
                <li key={itemIdx}>{renderInline(item, inverted)}</li>
              ))}
            </ListTag>
          );
        }

        return <div key={idx}>{renderParagraph(block.lines, compact, inverted)}</div>;
      })}
    </div>
  );
}
