import { describe, expect, it } from "vitest";

import { scalarText } from "./json";

describe("scalarText (R08.13)", () => {
  it("renders scalars as text", () => {
    expect(scalarText("RPM")).toBe("RPM");
    expect(scalarText("")).toBe("");
    expect(scalarText(3)).toBe("3");
    expect(scalarText(false)).toBe("false");
  });

  it("reads an object, array or nullish value as missing, not '[object Object]'", () => {
    expect(scalarText({ name: "x" })).toBeNull();
    expect(scalarText(["x"])).toBeNull();
    expect(scalarText(null)).toBeNull();
    expect(scalarText(undefined)).toBeNull();
  });
});
