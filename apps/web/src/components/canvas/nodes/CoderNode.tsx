import React, { memo } from "react";
import { type NodeProps } from "@xyflow/react";
import type { NodeData } from "@/types/workflow";
import BaseNode from "@/components/canvas/BaseNode";

/**
 * CoderNode — displays the model identifier as a small subtitle.
 */
const CoderNode = memo(function CoderNode(props: NodeProps) {
  const data = props.data as NodeData;

  return (
    <BaseNode {...props}>
      {data.modelId && (
        <span className="text-gray-400">{data.modelId}</span>
      )}
    </BaseNode>
  );
});

export default CoderNode;
