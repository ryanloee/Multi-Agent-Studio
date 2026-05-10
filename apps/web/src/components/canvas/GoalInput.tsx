"use client";
import { useState, useCallback, useRef, useEffect } from "react";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import { Target } from "lucide-react";

export default function GoalInput() {
  const goal = useWorkflowStore((s) => s.goal);
  const updateGoal = useWorkflowStore((s) => s.updateGoal);
  const t = useLocaleStore((s) => s.t);

  const [localGoal, setLocalGoal] = useState(goal);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setLocalGoal(goal);
  }, [goal]);

  const handleChange = useCallback(
    (value: string) => {
      setLocalGoal(value);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => updateGoal(value), 800);
    },
    [updateGoal]
  );

  // Clear debounce timer on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  return (
    <div className="flex-1 flex items-center justify-center bg-gradient-to-br from-slate-50 to-blue-50 p-8">
      <div className="w-full max-w-2xl space-y-6">
        <div className="text-center space-y-2">
          <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-blue-100 text-blue-700 text-xs font-medium">
            <Target size={14} />
            {t("workflow.modeAuto")}
          </div>
          <h2 className="text-2xl font-bold text-gray-800">
            {t("workflow.goalLabel")}
          </h2>
          <p className="text-sm text-gray-500">
            {t("workflow.modeAutoDesc")}
          </p>
        </div>
        <textarea
          value={localGoal}
          onChange={(e) => handleChange(e.target.value)}
          placeholder={t("workflow.goalPlaceholder")}
          rows={8}
          className="w-full rounded-xl border border-gray-200 bg-white px-5 py-4 text-base text-gray-800 placeholder-gray-300 shadow-sm resize-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all"
        />
      </div>
    </div>
  );
}
