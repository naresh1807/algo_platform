import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../services/api.js", () => ({
  endpoints: {
    setSelectedOptionExpiry: vi.fn(() => Promise.resolve({ data: { underlying: "NIFTY", selected_expiry: "2026-08-27" } })),
    optionExpiryStatus: vi.fn(),
  },
}));

import { endpoints } from "../services/api.js";
import { publishSelectedExpiry } from "./useExpiryStatus.js";

describe("publishSelectedExpiry", () => {
  beforeEach(() => {
    endpoints.setSelectedOptionExpiry.mockClear();
  });

  it("sends the correct underlying and expiry to apps.options.views.SelectedExpiryView", async () => {
    await publishSelectedExpiry("NIFTY", "2026-08-27");

    expect(endpoints.setSelectedOptionExpiry).toHaveBeenCalledTimes(1);
    expect(endpoints.setSelectedOptionExpiry).toHaveBeenCalledWith("NIFTY", "2026-08-27");
  });

  it("never throws when the backend call fails (best-effort publish)", async () => {
    endpoints.setSelectedOptionExpiry.mockReturnValueOnce(Promise.reject(new Error("network down")));

    await expect(publishSelectedExpiry("BANKNIFTY", "2026-08-27")).resolves.toBeUndefined();
  });
});
