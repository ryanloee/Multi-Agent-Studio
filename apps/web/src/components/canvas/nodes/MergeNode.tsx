import React, { memo } from "react";
import { type NodeProps } from "@xyflow/react";
import BaseNode from "@/components/canvas/BaseNode";
import { useRunStore } from "@/stores/runStore";

const MergeNode = memo(function MergeNode(props: NodeProps) {
  const nodeStatus = useRunStore(
    (state) => state.nodeStatuses[props.id] ?? "idle"
  );

  const statusLabels: Record<string, string> = {
    idle: "等待合并",
    running: "合并中",
    paused: "等待决策",
    completed: "已合并",
    failed: "合并失败",
  };

  return (
    <BaseNode {...props}>
      <span className="text-gray-500">{statusLabels[nodeStatus] ?? nodeStatus}</span>
    </BaseNode>
  );
});

export default MergeNode;
