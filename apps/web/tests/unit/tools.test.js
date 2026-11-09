import { describe, it, expect } from "vitest";
import {
  validateToolName,
  schemaRowsToJson,
  jsonToSchemaRows,
  validateToolForm,
  placeholdersOf,
} from "@/lib/tools";

describe("validateToolName", () => {
  it("matches the backend regex", () => {
    expect(validateToolName("lookup_order")).toBeNull();
    expect(validateToolName("ab")).toBeNull();
    for (const bad of ["", "a", "Order", "1abc", "has-dash", "x".repeat(65)]) {
      expect(validateToolName(bad)).toBeTruthy();
    }
    expect(validateToolName("x".repeat(64))).toBeNull();
  });
});

describe("schemaRowsToJson and back", () => {
  const rows = [
    { name: "order_id", type: "string", description: "The order", required: true, enum: "" },
    { name: "limit", type: "integer", description: "", required: false, enum: "" },
    { name: "kind", type: "string", description: "", required: false, enum: "a, b" },
  ];
  it("generates the JSON schema", () => {
    expect(schemaRowsToJson(rows)).toEqual({
      type: "object",
      properties: {
        order_id: { type: "string", description: "The order" },
        limit: { type: "integer" },
        kind: { type: "string", enum: ["a", "b"] },
      },
      required: ["order_id"],
    });
  });
  it("round-trips", () => {
    expect(jsonToSchemaRows(schemaRowsToJson(rows))).toEqual(rows);
  });
  it("parses numeric enums", () => {
    const json = schemaRowsToJson([{ name: "n", type: "number", description: "", required: false, enum: "1, 2.5" }]);
    expect(json.properties.n.enum).toEqual([1, 2.5]);
  });
  it("handles an empty schema", () => {
    expect(jsonToSchemaRows({ type: "object", properties: {} })).toEqual([]);
    expect(schemaRowsToJson([])).toEqual({ type: "object", properties: {}, required: [] });
  });
});

describe("validateToolForm", () => {
  const rows = [{ name: "order_id", type: "string", description: "", required: true, enum: "" }];
  const base = { name: "lookup_order", description: "Find an order", method: "GET", url_template: "https://x.io/o/{order_id}", rows };
  it("accepts a valid tool", () => {
    expect(validateToolForm(base)).toEqual({ ok: true, errors: {} });
  });
  it("requires placeholders to be required properties", () => {
    const r = validateToolForm({ ...base, rows: [{ ...rows[0], required: false }] });
    expect(r.errors.url_template).toMatch(/order_id/);
    expect(validateToolForm({ ...base, rows: [] }).errors.url_template).toBeTruthy();
  });
  it("limits properties to 20 and rejects duplicate or bad names", () => {
    const many = Array.from({ length: 21 }, (_, i) => ({ ...rows[0], name: `p${i}`, required: false }));
    expect(validateToolForm({ ...base, url_template: "https://x.io", rows: many }).errors.schema).toBeTruthy();
    expect(validateToolForm({ ...base, rows: [rows[0], rows[0]] }).errors.schema).toBeTruthy();
    expect(validateToolForm({ ...base, rows: [{ ...rows[0], name: "1x" }] }).errors.schema).toBeTruthy();
  });
  it("requires https/http url, description, method", () => {
    expect(validateToolForm({ ...base, url_template: "ftp://x" }).errors.url_template).toBeTruthy();
    expect(validateToolForm({ ...base, description: " " }).errors.description).toBeTruthy();
    expect(validateToolForm({ ...base, method: "TRACE" }).errors.method).toBeTruthy();
  });
  it("finds placeholders", () => {
    expect(placeholdersOf("/a/{x}/{y}/{x}")).toEqual(["x", "y"]);
  });
});

import { validateAuthHeader, validateToolArguments } from "@/lib/tools";

describe("validateAuthHeader", () => {
  it("requires a value for a new header name", () => {
    expect(validateAuthHeader({ name: "Authorization", value: "", previousName: null })).toBeTruthy();
    expect(validateAuthHeader({ name: "Authorization", value: "Bearer x", previousName: null })).toBeNull();
    expect(validateAuthHeader({ name: "", value: "", previousName: null })).toBeNull();
  });
  it("keeps the stored value when the header already existed", () => {
    expect(validateAuthHeader({ name: "X-Key", value: "", previousName: "Authorization" })).toBeNull();
  });
  it("requires the value again when the URL moves to another scheme, host or port", () => {
    const base = { name: "X-Key", value: "", previousName: "X-Key", previousUrl: "https://api.shop.com/o/{id}" };
    expect(validateAuthHeader({ ...base, url: "https://api.shop.com/v2/o/{id}" })).toBeNull();
    for (const url of ["https://evil.example.net/o/{id}", "https://api.shop.com:8443/o/{id}", "http://api.shop.com/o/{id}"]) {
      expect(validateAuthHeader({ ...base, url })).toMatch(/re-enter/i);
      expect(validateAuthHeader({ ...base, url, value: "Bearer new" })).toBeNull();
    }
    expect(validateAuthHeader({ ...base, name: "", url: "https://evil.example.net/o/{id}" })).toBeNull();
  });
});

describe("validateToolArguments", () => {
  const schema = {
    type: "object",
    properties: { id: { type: "string" }, n: { type: "integer" }, f: { type: "number" }, b: { type: "boolean" } },
    required: ["id"],
  };
  it("types valid input and skips blanks", () => {
    expect(validateToolArguments(schema, { id: "a1", n: "3", f: "2.5", b: "true" })).toEqual({
      args: { id: "a1", n: 3, f: 2.5, b: true },
      errors: {},
    });
    expect(validateToolArguments(schema, { id: "a1" }).args).toEqual({ id: "a1" });
  });
  it("reports missing required, bad numbers and booleans", () => {
    const { errors } = validateToolArguments(schema, { n: "1.5", f: "abc", b: "yes" });
    expect(Object.keys(errors).sort()).toEqual(["b", "f", "id", "n"]);
  });
});
