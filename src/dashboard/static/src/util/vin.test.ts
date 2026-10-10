import { describe, expect, it } from "vitest";

import { checkVin, expectedCheckDigit } from "./vin";

describe("checkVin (mirror of src/vehicle/vin.py)", () => {
  it("passes the textbook check-digit example", () => {
    expect(expectedCheckDigit("1M8GDM9AXKP042788")).toBe("X");
    expect(checkVin(" 1m8gdm9axkp042788 ").kind).toBe("pass");
  });

  it("refuses a North American check-digit failure", () => {
    expect(checkVin("1M8GDM9A1KP042788").kind).toBe("refused");
  });

  it("only warns on a non-North-American mismatch", () => {
    expect(checkVin("WVWZZZ1JZXW000001").kind).toBe("warning");
  });

  it("names the shape rule a malformed VIN breaks", () => {
    expect(checkVin("")).toEqual({ kind: "empty" });
    expect(checkVin("1M8GDM9AXKP04278")).toMatchObject({ kind: "malformed" });
    expect(checkVin("1M8GDM9AXKP04278O")).toMatchObject({
      kind: "malformed",
      message: expect.stringContaining("I, O or Q"),
    });
  });
});
