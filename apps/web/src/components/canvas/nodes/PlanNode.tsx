import React, { memo } from "react";
import { type NodeProps } from "@xyflow/react";
import type { NodeData } from "@/types/workflow";
import BaseNode from "@/components/canvas/BaseNode";

/**
 * PlanNode — displays a "Read-only" tag to indicate this is a planning node.
 */
const PlanNode = memo(function PlanNode(props: NodeProps) {
  return (
    <BaseNode {...props}>
      <span className="inline-block rounded bg-green-100 px-1.5 py-0.5 text-green-700 text-xs font-medium">
        只读模式
      </span>
    </BaseNode>
  );
});

export default PlanNode;
