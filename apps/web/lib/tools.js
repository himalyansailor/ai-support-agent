// Pure helpers for the business tools page. Mirrors supportcore.tools (schema + service);
// the server stays the source of truth and its errors are always shown.

export const TOOL_METHODS = ["GET", "POST"];
export const PROPERTY_TYPES = ["string", "number", "integer", "boolean"];
export const MAX_PROPERTIES = 20;
export const MAX_DESCRIPTION_LENGTH = 1000;
export const MAX_URL_LENGTH = 2000;
export const WRITE_WARNING = "Calls to this tool will always hand the conversation to a person.";

const NAME = /^[a-z][a-z0-9_]{1,63}$/;
const PROPERTY_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

// -> error message, or null.
export function validateToolName(name) {
  return NAME.test(name ?? "")
    ? null
    : "Name must be 2-64 characters: lowercase letters, digits and underscores, starting with a letter.";
}

export function placeholdersOf(urlTemplate) {
  return [...new Set([...urlTemplate.matchAll(/\{([^{}]*)\}/g)].map((m) => m[1]))];
}

export const emptyRow = () => ({ name: "", type: "string", description: "", required: false, enum: "" });

function parseEnum(text, type) {
  const parts = text.split(",").map((s) => s.trim()).filter(Boolean);
  if (type === "string") return parts;
  if (type === "boolean") return parts.map((p) => p === "true");
  return parts.map(Number);
}

// Rows UI -> the input_schema JSON.
export function schemaRowsToJson(rows) {
  const properties = {};
  const required = [];
  for (const row of rows) {
    const prop = { type: row.type };
    if (row.description.trim()) prop.description = row.description.trim();
    if (row.enum.trim()) prop.enum = parseEnum(row.enum, row.type);
    properties[row.name] = prop;
    if (row.required) required.push(row.name);
  }
  return { type: "object", properties, required };
}

// input_schema JSON -> rows UI.
export function jsonToSchemaRows(schema) {
  const required = new Set(schema?.required ?? []);
  return Object.entries(schema?.properties ?? {}).map(([name, prop]) => ({
    name,
    type: prop.type,
    description: prop.description ?? "",
    required: required.has(name),
    enum: Array.isArray(prop.enum) ? prop.enum.join(", ") : "",
  }));
}

function validateRows(rows) {
  if (rows.length > MAX_PROPERTIES) return `A tool can have at most ${MAX_PROPERTIES} properties.`;
  const seen = new Set();
  for (const row of rows) {
    if (!PROPERTY_NAME.test(row.name)) {
      return "Property names must be letters, digits and underscores, starting with a letter or underscore.";
    }
    if (seen.has(row.name)) return `Property ${row.name} appears twice.`;
    seen.add(row.name);
    if (!PROPERTY_TYPES.includes(row.type)) return `Property ${row.name} needs a type.`;
    if (row.enum.trim()) {
      const values = parseEnum(row.enum, row.type);
      if (values.length > 50) return `Property ${row.name} can list at most 50 values.`;
      if (row.type !== "string" && row.type !== "boolean" && values.some((v) => !Number.isFinite(v))) {
        return `Property ${row.name} allowed values must be numbers.`;
      }
      if (row.type === "integer" && values.some((v) => !Number.isInteger(v))) {
        return `Property ${row.name} allowed values must be whole numbers.`;
      }
    }
  }
  return null;
}

// -> { ok, errors: { name?, description?, method?, url_template?, schema? } }
export function validateToolForm({ name, description, method, url_template, rows }) {
  const errors = {};
  const nameError = validateToolName(name);
  if (nameError) errors.name = nameError;
  const desc = (description ?? "").trim();
  if (!desc || desc.length > MAX_DESCRIPTION_LENGTH) {
    errors.description = `Description must be 1-${MAX_DESCRIPTION_LENGTH} characters.`;
  }
  if (!TOOL_METHODS.includes(method)) errors.method = `Method must be ${TOOL_METHODS.join(" or ")}.`;
  const schemaError = validateRows(rows);
  if (schemaError) errors.schema = schemaError;

  const url = (url_template ?? "").trim();
  if (!/^https?:\/\/\S+$/.test(url) || url.length > MAX_URL_LENGTH) {
    errors.url_template = "Enter a full http:// or https:// URL.";
  } else {
    const stripped = url.replace(/\{[^{}]*\}/g, "");
    if (stripped.includes("{") || stripped.includes("}")) {
      errors.url_template = "The URL has an unmatched brace.";
    } else {
      const required = new Set(rows.filter((r) => r.required).map((r) => r.name));
      const known = new Set(rows.map((r) => r.name));
      for (const p of placeholdersOf(url)) {
        if (!known.has(p)) {
          errors.url_template = `{${p}} must be an input property.`;
          break;
        }
        if (!required.has(p)) {
          errors.url_template = `{${p}} must be a required property.`;
          break;
        }
      }
    }
  }
  return { ok: Object.keys(errors).length === 0, errors };
}

// -> error message or null. A header name needs a value when creating, or when the name changes
// and no header was set before (an existing header keeps its stored value if left empty).
// Scheme, host and port of a URL template ("" when it does not parse).
function urlOrigin(template) {
  try {
    return new URL(String(template ?? "").trim().replace(/\{[^}]*\}/g, "x")).origin;
  } catch {
    return "";
  }
}

export function validateAuthHeader({ name, value, previousName, url, previousUrl }) {
  const n = (name ?? "").trim();
  if (!n) return null;
  if (!/^[A-Za-z0-9-]{1,100}$/.test(n)) return "Auth header name may only contain letters, digits and dashes.";
  if (value) return null;
  if (!previousName) return "Enter the auth header value.";
  // The server drops the stored value when the URL moves to another scheme, host or port.
  if (previousUrl !== undefined && urlOrigin(url) !== urlOrigin(previousUrl)) {
    return "The URL now points to a different host: re-enter the auth header value.";
  }
  return null;
}

// Typed arguments from text fields (blank optional fields left out).
// -> { args, errors: { [name]: message } }
export function validateToolArguments(schema, values) {
  const args = {};
  const errors = {};
  const required = new Set(schema?.required ?? []);
  for (const [name, prop] of Object.entries(schema?.properties ?? {})) {
    const raw = String(values[name] ?? "");
    const text = raw.trim();
    if (text === "") {
      if (required.has(name)) errors[name] = "Required.";
      continue;
    }
    if (prop.type === "number" || prop.type === "integer") {
      const n = Number(text);
      if (!Number.isFinite(n)) errors[name] = "Enter a number.";
      else if (prop.type === "integer" && !Number.isInteger(n)) errors[name] = "Enter a whole number.";
      else args[name] = n;
    } else if (prop.type === "boolean") {
      if (text === "true" || text === "false") args[name] = text === "true";
      else errors[name] = "Enter true or false.";
    } else {
      args[name] = raw;
    }
    if (Array.isArray(prop.enum) && name in args && !prop.enum.includes(args[name])) {
      errors[name] = `Must be one of: ${prop.enum.join(", ")}.`;
    }
  }
  return { args, errors };
}
