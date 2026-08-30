/* API endpoint resolution.
 *
 * The UI can be served two ways:
 *   1. by the Fly app itself (same origin) — nothing to configure;
 *   2. from GitHub Pages, with the API on Fly — a cross-origin base URL is needed.
 *
 * Resolution order: a value saved in this browser > the baked-in default below >
 * same origin. Edit DEFAULT_API_BASE before deploying to Pages so visitors don't
 * each have to set it by hand.
 */

const DEFAULT_API_BASE = ""; // e.g. "https://your-app.fly.dev"

export const Config = {
  get apiBase() {
    const saved = localStorage.getItem("bookshelf.apiBase");
    return (saved || DEFAULT_API_BASE || "").replace(/\/$/, "");
  },
  set apiBase(value) {
    const cleaned = (value || "").trim().replace(/\/$/, "");
    if (cleaned) localStorage.setItem("bookshelf.apiBase", cleaned);
    else localStorage.removeItem("bookshelf.apiBase");
  },
  get token() {
    return localStorage.getItem("bookshelf.token") || "";
  },
  set token(value) {
    const cleaned = (value || "").trim();
    if (cleaned) localStorage.setItem("bookshelf.token", cleaned);
    else localStorage.removeItem("bookshelf.token");
  },
  /** True when the UI is served from somewhere other than the API (Pages). */
  get isSplitDeploy() {
    return Boolean(this.apiBase) && !this.apiBase.startsWith(location.origin);
  },
};

export function apiUrl(path) {
  return `${Config.apiBase}${path}`;
}

/** Shared fetch wrapper: attaches the token and turns error bodies into messages. */
export async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (Config.token) headers["X-Bookshelf-Token"] = Config.token;

  let response;
  try {
    response = await fetch(apiUrl(path), { ...options, headers });
  } catch {
    // A cross-origin failure surfaces here as an opaque TypeError.
    throw new Error(
      Config.isSplitDeploy
        ? "Could not reach the API. Check the server URL and that this page's origin is allowed by CORS."
        : "Could not reach the API. Is the server running?"
    );
  }

  if (response.status === 401) {
    throw new Error("Unauthorized — set your access token in Settings.");
  }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}
