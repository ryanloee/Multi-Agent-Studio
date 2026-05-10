"use client";

import { useCallback } from "react";
import { useLocaleStore } from "@/stores/localeStore";

// ---------------------------------------------------------------------------
// Standard tool identifiers that can be permission-controlled
// ---------------------------------------------------------------------------
const TOOLS = [
  "file_read",
  "file_write",
  "shell_exec",
  "web_search",
  "browser",
  "mcp",
] as const;

type PermissionValue = "allow" | "deny" | "ask";

interface PermissionsEditorProps {
  value: Record<string, PermissionValue>;
  onChange: (v: Record<string, PermissionValue>) => void;
  disabled?: boolean;
}

export default function PermissionsEditor({
  value,
  onChange,
  disabled,
}: PermissionsEditorProps) {
  const t = useLocaleStore((s) => s.t);

  const handleChange = useCallback(
    (tool: string, perm: PermissionValue) => {
      onChange({ ...value, [tool]: perm });
    },
    [value, onChange],
  );

  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide">
        {t("permissions.label")}
      </label>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-400">
            <th className="text-left font-normal pb-1">{t("permissions.tool")}</th>
            <th className="text-center font-normal pb-1 w-16">{t("permissions.allow")}</th>
            <th className="text-center font-normal pb-1 w-16">{t("permissions.deny")}</th>
            <th className="text-center font-normal pb-1 w-16">{t("permissions.ask")}</th>
          </tr>
        </thead>
        <tbody>
          {TOOLS.map((tool) => {
            const current: PermissionValue = value[tool] ?? "ask";
            return (
              <tr key={tool} className="border-t border-gray-100">
                <td className="py-1.5 text-gray-700 font-mono text-xs">
                  {tool}
                </td>
                {(["allow", "deny", "ask"] as const).map((perm) => (
                  <td key={perm} className="text-center py-1.5">
                    <input
                      type="radio"
                      name={`perm-${tool}`}
                      checked={current === perm}
                      onChange={() => handleChange(tool, perm)}
                      disabled={disabled}
                      className="accent-blue-500 disabled:opacity-50"
                    />
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
