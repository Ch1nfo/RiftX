/** @type {import('next').NextConfig} */
const nextConfig = {
  typedRoutes: true,
  devIndicators: false,
  serverExternalPackages: [
    "@mariozechner/pi-coding-agent",
    "@mariozechner/pi-ai"
  ]
};

export default nextConfig;
