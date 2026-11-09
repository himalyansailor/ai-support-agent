/** @type {import('next').NextConfig} */
const nextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  // The Phase 3 conversation pages became the inbox.
  async redirects() {
    return [
      { source: "/orgs/:orgId/conversations", destination: "/orgs/:orgId/inbox", permanent: false },
      {
        source: "/orgs/:orgId/conversations/:conversationId",
        destination: "/orgs/:orgId/inbox/:conversationId",
        permanent: false,
      },
    ];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
