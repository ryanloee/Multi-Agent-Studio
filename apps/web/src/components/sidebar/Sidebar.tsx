"use client";

import { type DragEvent } from "react";
import type { AgentNodeType } from "@/types/workflow";
import { NODE_META } from "@/lib/constants";
import { useLocaleStore } from "@/stores/localeStore";

// ---------------------------------------------------------------------------
// Icon mapping — SVG glyphs matching NODE_META icon names
// ---------------------------------------------------------------------------
const ICON_MAP: Record<string, JSX.Element> = {
  Code: (
    <svg viewBox="0 0 24 24" className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2}>
      <polyline points="16 18 22 12 16 6" />
      <polyline points="8 6 2 12 8 18" />
    </svg>
  ),
  Map: (
    <svg viewBox="0 0 24 24" className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2}>
      <polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6" />
      <line x1="8" y1="2" x2="8" y2="18" />
      <line x1="16" y1="6" x2="16" y2="22" />
    </svg>
  ),
  Search: (
    <svg viewBox="0 0 24 24" className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2}>
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  ),
  Terminal: (
    <svg viewBox="0 0 24 24" className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2}>
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  ),
  FileCheck: (
    <svg viewBox="0 0 24 24" className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
      <polyline points="10 9 9 9 8 9" />
    </svg>
  ),
  User: (
    <svg viewBox="0 0 24 24" className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2}>
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  ),
};

// ---------------------------------------------------------------------------
// Color mapping for icon tinting
// ---------------------------------------------------------------------------
const COLOR_MAP: Record<string, string> = {
  blue: "#3b82f6",
  green: "#22c55e",
  yellow: "#eab308",
  gray: "#6b7280",
  purple: "#a855f7",
  orange: "#f97316",
};

// ---------------------------------------------------------------------------
// Node type list to render (stable order)
// ---------------------------------------------------------------------------
const NODE_ORDER: AgentNodeType[] = ["coder", "plan", "explore", "shell", "review", "human"];

// ---------------------------------------------------------------------------
// Sidebar component
// ---------------------------------------------------------------------------
export default function Sidebar() {
  const t = useLocaleStore((s) => s.t);

  const onDragStart = (event: DragEvent<HTMLDivElement>, nodeType: AgentNodeType) => {
    event.dataTransfer.setData("application/reactflow", nodeType);
    event.dataTransfer.effectAllowed = "move";
  };

  return (
    <aside className="w-[240px] h-full bg-white border-r border-gray-200 flex flex-col overflow-hidden shrink-0">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-100">
        <h2 className="text-sm font-semibold text-gray-700">{t("sidebar.title")}</h2>
        <p className="text-xs text-gray-400 mt-0.5">{t("sidebar.dragToCanvas")}</p>
      </div>

      {/* Node cards */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {NODE_ORDER.map((type) => {
          const meta = NODE_META[type];
          const icon = ICON_MAP[meta.icon] ?? ICON_MAP.Code;
          const color = COLOR_MAP[meta.color] ?? "#6b7280";

          return (
            <div
              key={type}
              draggable
              onDragStart={(e) => onDragStart(e, type)}
              className="flex items-start gap-3 p-3 rounded-lg border border-gray-150 bg-gray-50 cursor-grab hover:bg-gray-100 hover:border-gray-300 hover:shadow-sm transition-all select-none active:cursor-grabbing"
            >
              {/* Icon */}
              <div
                className="flex items-center justify-center w-8 h-8 rounded-md shrink-0"
                style={{ backgroundColor: `${color}18` }}
              >
                <span style={{ color }}>{icon}</span>
              </div>

              {/* Text */}
              <div className="min-w-0">
                <p className="text-sm font-medium text-gray-800 leading-tight">
                  {t(`node.${type}.label`)}
                </p>
                <p className="text-xs text-gray-400 mt-0.5 leading-snug line-clamp-2">
                  {t(`node.${type}.description`)}
                </p>
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}
