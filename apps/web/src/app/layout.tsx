import type { Metadata } from "next";
import AuthGate from "@/components/auth/AuthGate";
import "./globals.css";

export const metadata: Metadata = {
  title: "Multi-Agent Studio",
  description: "AI Multi-Agent Workflow Orchestration OS",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh" className="h-full">
      <body className="h-full antialiased text-gray-900 bg-gray-50">
        <AuthGate>{children}</AuthGate>
      </body>
    </html>
  );
}
