import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";

// Self-hosted variable-weight mono font: Linux has none of the macOS/Windows
// families in the CSS stack, so the UI previously fell back to whatever
// fontconfig picked (usually DejaVu Sans Mono, weight 400 only) — every
// 550/650 weight collapsed to thin 400. Bundling JetBrains Mono (100-800)
// makes rendering identical on every platform. CJK glyphs are deliberately
// not bundled (multi-MB) and resolve through the explicit system chain.
const jetbrainsMono = localFont({
  src: [
    { path: "./fonts/JetBrainsMono-Variable-latin.woff2", style: "normal" },
    { path: "./fonts/JetBrainsMono-Variable-latin-ext.woff2", style: "normal" }
  ],
  weight: "100 800",
  display: "swap",
  variable: "--font-jetbrains-mono",
  preload: true
});

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
    <html lang="zh-CN" data-design="terminal" className={jetbrainsMono.variable} suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeInitScript }} /></head>
      <body>{children}</body>
    </html>
  );
}
