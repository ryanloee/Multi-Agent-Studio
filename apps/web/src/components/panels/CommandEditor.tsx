"use client";

import { useLocaleStore } from "@/stores/localeStore";

interface CommandEditorProps {
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
}

export default function CommandEditor({ value, onChange, disabled }: CommandEditorProps) {
  const t = useLocaleStore((s) => s.t);

  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide">
        {t("command.label")}
      </label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={t("command.placeholder")}
        disabled={disabled}
        className="w-full rounded-md border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-800 placeholder-gray-300 font-mono focus:border-blue-400 focus:ring-1 focus:ring-blue-400 focus:outline-none disabled:opacity-50 disabled:cursor-not-allowed"
      />
    </div>
  );
}
