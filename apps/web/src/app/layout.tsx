import type { Metadata } from "next";

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
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
