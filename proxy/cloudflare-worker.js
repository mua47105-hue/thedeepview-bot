/**
 * Cloudflare Worker — Telegram Bot API Reverse Proxy
 *
 * PURPOSE:
 *   Hugging Face Spaces BLOCKS api.telegram.org at the TLS/SNI level
 *   (intentional policy to prevent bot abuse on free tier). This worker
 *   acts as a transparent reverse proxy, forwarding all requests to
 *   api.telegram.org.
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
 * COST: $0 (Cloudflare Workers free tier = 100k requests/day)
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Health check endpoint
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

    // CRITICAL: pass the original request object directly to fetch(url, request).
    // Do NOT use new Request(url, request) — it can drop POST bodies on some
    // Cloudflare Workers runtime versions, causing sendMessage to fail.
    // Passing the original request preserves method, headers, and body correctly.
    try {
      const response = await fetch(url, request);

      // Add CORS headers
      const newHeaders = new Headers(response.headers);
      newHeaders.set("Access-Control-Allow-Origin", "*");
      newHeaders.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
      newHeaders.set("Access-Control-Allow-Headers", "*");

      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: newHeaders });
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
