import React, { memo } from "react";
import { type NodeProps } from "@xyflow/react";
import type { NodeData } from "@/types/workflow";
import BaseNode from "@/components/canvas/BaseNode";

/**
 * HumanNode — basic node, shows description if available.
 */
const HumanNode = memo(function HumanNode(props: NodeProps) {
  const data = props.data as NodeData;

  return (
    <BaseNode {...props}>
      {data.description ? (
        <span className="truncate">{data.description}</span>
      ) : null}
    </BaseNode>
  );
});

export default HumanNode;
