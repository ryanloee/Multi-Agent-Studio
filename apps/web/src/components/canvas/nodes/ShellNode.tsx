import React, { memo } from "react";
import { type NodeProps } from "@xyflow/react";
import type { NodeData } from "@/types/workflow";
import BaseNode from "@/components/canvas/BaseNode";

/**
 * ShellNode — previews the first 30 characters of the command.
 */
const ShellNode = memo(function ShellNode(props: NodeProps) {
  const data = props.data as NodeData;

  const command = data.command || "";
  const preview = command.length > 30 ? command.slice(0, 30) + "..." : command;

  return (
    <BaseNode {...props}>
      {preview ? (
        <code className="block truncate rounded bg-gray-100 px-1.5 py-0.5 font-mono text-gray-600">
          {preview}
        </code>
      ) : (
        <span className="italic text-gray-400">未设置命令</span>
      )}
    </BaseNode>
  );
});

export default ShellNode;
