import React, { memo, useMemo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import type { RunStatus } from "@/types/workflow";

/**
 * ChildNode — dynamically created child task node from a planner.
 * Smaller than regular nodes, shows type badge + truncated prompt + status.
 */

const TYPE_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  coder:   { bg: "bg-blue-50",  text: "text-blue-600",  border: "border-blue-200" },
  design:  { bg: "bg-emerald-50", text: "text-emerald-600", border: "border-emerald-200" },
  explore: { bg: "bg-amber-50", text: "text-amber-600", border: "border-amber-200" },
  merge:   { bg: "bg-teal-50",  text: "text-teal-600",  border: "border-teal-200" },
  shell:   { bg: "bg-gray-100", text: "text-gray-600",  border: "border-gray-200" },
  review:  { bg: "bg-purple-50", text: "text-purple-600", border: "border-purple-200" },
};

const STATUS_DOT: Record<string, string> = {
  running:   "bg-blue-500 animate-pulse",
  completed: "bg-green-500",
  failed:    "bg-red-500",
};

const ChildNode = memo(function ChildNode({ id, data, selected }: NodeProps) {
  const childType = (data as Record<string, unknown>).childType as string || "coder";
  const prompt = (data as Record<string, unknown>).childPrompt as string || "";
  const t = useLocaleStore((s) => s.t);

  const status: RunStatus = useRunStore(
    (state) => state.nodeStatuses[id] ?? "idle"
  );

  const colors = TYPE_COLORS[childType] ?? TYPE_COLORS.coder;
  const dotClass = STATUS_DOT[status] ?? "bg-gray-300";

  const truncatedPrompt = useMemo(() => {
    if (!prompt) return "";
    return prompt.length > 60 ? prompt.slice(0, 57) + "..." : prompt;
  }, [prompt]);

  return (
    <div
      className={[
        "relative rounded-md border bg-white shadow-sm transition-shadow overflow-hidden",
        selected ? "ring-2 ring-blue-500" : "",
        colors.border,
      ].join(" ")}
      style={{ width: 180 }}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="!w-2.5 !h-2.5 !bg-gray-400 !border-2 !border-white"
      />

      {/* Type badge bar */}
      <div className={`h-1 ${colors.bg}`} />

      <div className="px-2.5 py-1.5">
        {/* Header: status dot + type label */}
        <div className="flex items-center gap-1.5 mb-1">
          <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotClass}`} />
          <span className={`text-xs font-semibold ${colors.text}`}>
            {t(`node.${childType}.label`) || childType}
          </span>
          <span className="text-[10px] text-gray-400 ml-auto font-mono">
            {childType}
          </span>
        </div>

        {/* Prompt preview */}
        {truncatedPrompt && (
          <p className="text-[11px] text-gray-500 leading-snug line-clamp-2">
            {truncatedPrompt}
          </p>
        )}

        {/* Status text */}
        {status !== "idle" && (
          <div className="mt-1">
            <span className={`text-[10px] font-medium ${
              status === "running" ? "text-blue-500" :
              status === "completed" ? "text-green-500" :
              status === "failed" ? "text-red-500" : "text-gray-400"
            }`}>
              {status}
            </span>
          </div>
        )}
      </div>

      <Handle
        type="source"
        position={Position.Bottom}
        className="!w-2.5 !h-2.5 !bg-gray-400 !border-2 !border-white"
      />
    </div>
  );
});

export default ChildNode;
