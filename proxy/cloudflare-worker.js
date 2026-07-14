/**
 * Cloudflare Worker — Telegram Bot API Reverse Proxy
 *
 * PURPOSE:
 *   Hugging Face Spaces BLOCKS api.telegram.org at the TLS/SNI level
 *   (intentional policy to prevent bot abuse on free tier). This worker
 *   acts as a transparent reverse proxy, forwarding all requests to
 *   api.telegram.org. Deploy this on Cloudflare Workers (free tier:
 *   100,000 requests/day — more than enough for a bot that sends ~15
 *   messages every 2 hours = ~180 messages/day).
 *
 * DEPLOY (5 minutes):
 *   1. Go to https://dash.cloudflare.com → Workers & Pages → Create
 *   2. Name it "tg-proxy" (or whatever)
 *   3. Paste this entire file into the editor
 *   4. Click "Deploy"
 *   5. Copy the worker URL (e.g. https://tg-proxy.your-name.workers.dev)
 *   6. In your HF Space Settings → Repository secrets, add:
 *        TELEGRAM_API_BASE = https://tg-proxy.your-name.workers.dev
 *   7. Restart the Space (or wait for next pipeline run)
 *
 * ALTERNATIVE: deploy via Wrangler CLI
 *   npx wrangler deploy proxy/cloudflare-worker.js --name tg-proxy
 *
 * SECURITY:
 *   This proxy is open (no auth). For a private proxy, add a secret header
 *   check (uncomment the AUTH_TOKEN section below and set a secret in
 *   Cloudflare dashboard → Settings → Variables). For most personal bots,
 *   the open proxy is fine — your bot token is in the URL path, which
 *   isn't logged by Cloudflare.
 *
 * COST: $0 (Cloudflare Workers free tier = 100k requests/day)
 */

export default {
  async fetch(request, env) {
    // ── Optional: require auth token (uncomment to enable) ───────────────
    // if (env.AUTH_TOKEN) {
    //   const auth = request.headers.get("X-Proxy-Auth");
    //   if (auth !== env.AUTH_TOKEN) {
    //     return new Response("Unauthorized", { status: 401 });
    //   }
    // }

    const url = new URL(request.url);

    // Health check endpoint — useful for uptime monitors
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response(JSON.stringify({
        status: "ok",
        service: "telegram-bot-api-proxy",
        upstream: "api.telegram.org",
        timestamp: new Date().toISOString(),
      }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // Rewrite the host to api.telegram.org, keep everything else
    url.host = "api.telegram.org";
    url.protocol = "https:";

    // Clone the request with the new URL
    const proxyRequest = new Request(url, request);

    // Forward the request to Telegram
    try {
      const response = await fetch(proxyRequest);

      // Add CORS headers so browser-based tools can also use this proxy
      const newHeaders = new Headers(response.headers);
      newHeaders.set("Access-Control-Allow-Origin", "*");
      newHeaders.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
      newHeaders.set("Access-Control-Allow-Headers", "*");

      // Handle preflight requests
      if (request.method === "OPTIONS") {
        return new Response(null, {
          status: 204,
          headers: newHeaders,
        });
      }

      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: newHeaders,
      });
    } catch (error) {
      return new Response(JSON.stringify({
        error: "Failed to reach api.telegram.org",
        message: error.message,
      }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};
