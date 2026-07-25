/** @jest-environment node */

import { GET } from "./route";

describe("health route", () => {
  it("reports a healthy research-assistant-web status as JSON", async () => {
    const response = GET();

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("application/json");
    await expect(response.json()).resolves.toEqual({
      status: "healthy",
      service: "research-assistant-web",
    });
  });
});
