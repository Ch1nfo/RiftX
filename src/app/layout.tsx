import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "RiftX",
  description: "A focused RiftX security testing agent.",
  icons: { icon: "/riftx-logo-dark.png", apple: "/riftx-logo-dark.png" }
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" data-design="terminal">
      <body>{children}</body>
    </html>
  );
}
