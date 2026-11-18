import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pin the workspace root: a stray package-lock.json in a parent directory
  // otherwise makes Turbopack infer the wrong root.
  turbopack: { root: __dirname },
  // Every page here is static content, so build a plain static export.
  // `out/` can then be served by any static host and is what the local
  // preview serves.
  output: "export",
  images: { unoptimized: true },
};

export default nextConfig;
