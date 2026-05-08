import React, { memo } from "react";
import { type NodeProps } from "@xyflow/react";
import type { NodeData } from "@/types/workflow";
import BaseNode from "@/components/canvas/BaseNode";
import { useRunStore } from "@/stores/runStore";

/**
 * ReviewNode — displays the current status as a text label.
 */
const ReviewNode = memo(function ReviewNode(props: NodeProps) {
  const nodeStatus = useRunStore(
    (state) => state.nodeStatuses[props.id] ?? "idle"
  );

  const statusLabels: Record<string, string> = {
    idle: "等待中",
    running: "审查中",
    paused: "等待审批",
    completed: "已审查",
    failed: "审查失败",
  };

  return (
    <BaseNode {...props}>
      <span className="text-gray-500">{statusLabels[nodeStatus] ?? nodeStatus}</span>
    </BaseNode>
  );
});

export default ReviewNode;
