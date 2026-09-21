/** @type {import('next').NextConfig} */
const nextConfig = {
  typedRoutes: true,
  devIndicators: false,
  serverExternalPackages: [
    "better-sqlite3",
    "@mariozechner/pi-coding-agent",
    "@mariozechner/pi-ai"
  ]
};

export default nextConfig;
