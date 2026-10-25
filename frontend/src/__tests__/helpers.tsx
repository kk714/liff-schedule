import { vi } from "vitest";

import type { LiffClient } from "../liff";

type Handler = (body: unknown, url: URL) => { status?: number; json: unknown };

/** 以「METHOD /path」為鍵的假後端；記錄每次呼叫。 */
export function mockApi(routes: Record<string, Handler | unknown>) {
  const calls: { method: string; path: string; body: unknown; headers: Record<string, string> }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "https://app.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path: url.pathname, body, headers: (init?.headers ?? {}) as Record<string, string> });
    const route = routes[`${method} ${url.pathname}`];
    if (route === undefined) return new Response(JSON.stringify({ error: "not_found" }), { status: 404 });
    const result = typeof route === "function" ? (route as Handler)(body, url) : { json: route };
    return new Response(JSON.stringify(result.json), { status: result.status ?? 200 });
  });
  return calls;
}

export function fakeLiff(overrides: Partial<LiffClient> = {}): LiffClient {
  return {
    init: vi.fn(async () => {}),
    isLoggedIn: () => true,
    login: vi.fn(),
    getIDToken: () => "id-token-123",
    closeWindow: vi.fn(),
    isInClient: () => true,
    ...overrides,
  };
}

export const USER = { id: "u1", display_name: "王小明", timezone: "Asia/Taipei", default_visibility: "busy_only" };
export const ME = { user: USER, teams: [{ id: "t1", name: "業務部" }] };
export const CONFIG = { liff_id: "1650000000-test" };

export function at(path: string) {
  window.history.pushState(null, "", path);
}
