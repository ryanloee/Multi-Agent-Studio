"use client";

import { useLocaleStore } from "@/stores/localeStore";

interface PromptEditorProps {
  value: string;
  onChange: (prompt: string) => void;
  maxLength?: number;
}

export default function PromptEditor({ value, onChange, maxLength = 8000 }: PromptEditorProps) {
  const t = useLocaleStore((s) => s.t);
  const charCount = value.length;
  const isNearLimit = charCount > maxLength * 0.9;
  const isOverLimit = charCount > maxLength;

  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide">
        {t("prompt.label")}
      </label>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={t("prompt.placeholder")}
        rows={6}
        className="w-full rounded-md border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 placeholder-gray-300 resize-y focus:border-blue-400 focus:ring-1 focus:ring-blue-400 focus:outline-none"
      />
      <div className="flex justify-end">
        <span
          className={[
            "text-xs",
            isOverLimit
              ? "text-red-500 font-medium"
              : isNearLimit
                ? "text-yellow-500"
                : "text-gray-400",
          ].join(" ")}
        >
          {charCount.toLocaleString()} / {maxLength.toLocaleString()}
        </span>
      </div>
    </div>
  );
}
