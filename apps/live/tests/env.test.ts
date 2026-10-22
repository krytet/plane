/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

const BASE_ENV = {
  API_BASE_URL: "http://127.0.0.1:8059",
  LIVE_SERVER_SECRET_KEY: "test-secret",
};

describe("live env", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("accepts an explicit loopback bind host", async () => {
    vi.stubEnv("HOST", "127.0.0.1");
    Object.entries(BASE_ENV).forEach(([key, value]) => vi.stubEnv(key, value));

    const { env } = await import("../src/env");

    expect(env.HOST).toBe("127.0.0.1");
  });
});
