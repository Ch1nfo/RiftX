import type { Metadata } from "next";
import "./globals.css";

const themeInitScript = `(function () {
  try {
    var theme = window.localStorage.getItem("riftx-theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  } catch (_) {}
})();`;

export const metadata: Metadata = {
  title: "RiftX",
  description: "A focused RiftX security testing agent.",
  icons: { icon: "/riftx-logo-dark.png", apple: "/riftx-logo-dark.png" }
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" data-design="terminal" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeInitScript }} /></head>
      <body>{children}</body>
    </html>
  );
}
