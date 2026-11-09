import { describe, it, expect, vi } from "vitest";
import { createApiClient, ApiError } from "@/lib/api";

const BASE = "http://api.test";

function jsonResponse(status, body) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setup(handler) {
  const fetchMock = vi.fn(handler);
  vi.stubGlobal("fetch", fetchMock);
  let token = "old";
  const onAuthFailure = vi.fn();
  const client = createApiClient({
    baseUrl: BASE,
    getAccessToken: () => token,
    setAccessToken: (t) => {
      token = t;
    },
    onAuthFailure,
  });
  return { client, fetchMock, onAuthFailure, getToken: () => token };
}

describe("api client", () => {
  it("refreshes once and retries on 401", async () => {
    const { client, fetchMock, getToken } = setup((url, init) => {
      if (url === `${BASE}/auth/refresh`) {
        return jsonResponse(200, { access_token: "new", user: { id: "u" } });
      }
      const auth = init.headers.Authorization;
      return auth === "Bearer new"
        ? jsonResponse(200, { ok: true })
        : jsonResponse(401, { detail: "expired" });
    });

    const result = await client.get("/me");

    expect(result).toEqual({ ok: true });
    expect(getToken()).toBe("new");
    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls).toEqual([`${BASE}/me`, `${BASE}/auth/refresh`, `${BASE}/me`]);
    expect(fetchMock.mock.calls[0][1].credentials).toBe("include");
    expect(fetchMock.mock.calls[1][1].method).toBe("POST");
  });

  it("concurrent 401s trigger a single refresh", async () => {
    const { client, fetchMock } = setup(async (url, init) => {
      if (url === `${BASE}/auth/refresh`) {
        await new Promise((r) => setTimeout(r, 10));
        return jsonResponse(200, { access_token: "new" });
      }
      return init.headers.Authorization === "Bearer new"
        ? jsonResponse(200, { path: url })
        : jsonResponse(401, { detail: "expired" });
    });

    const results = await Promise.all([
      client.get("/a"),
      client.get("/b"),
      client.get("/c"),
    ]);

    expect(results.map((r) => r.path)).toEqual([
      `${BASE}/a`,
      `${BASE}/b`,
      `${BASE}/c`,
    ]);
    const refreshCalls = fetchMock.mock.calls.filter(
      (c) => c[0] === `${BASE}/auth/refresh`,
    );
    expect(refreshCalls).toHaveLength(1);
  });

  it("calls onAuthFailure when refresh fails", async () => {
    const { client, onAuthFailure } = setup((url) =>
      url === `${BASE}/auth/refresh`
        ? jsonResponse(401, { detail: "bad refresh" })
        : jsonResponse(401, { detail: "expired" }),
    );

    await expect(client.get("/me")).rejects.toMatchObject({ status: 401 });
    expect(onAuthFailure).toHaveBeenCalledTimes(1);
  });

  it("throws ApiError with status and detail", async () => {
    const { client } = setup(() => jsonResponse(409, { detail: "Email taken" }));

    const err = await client.post("/auth/signup", { email: "a@b.c" }).catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.detail).toBe("Email taken");
  });

  it("exposes the machine-readable error code", async () => {
    const { client } = setup(() =>
      jsonResponse(409, { detail: "An account with this email exists", code: "invite_account_exists" }),
    );

    const err = await client.post("/invites/accept", { token: "t" }).catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("invite_account_exists");
    expect(err.detail).toBe("An account with this email exists");
  });

  it("leaves code null when the server sends none", async () => {
    const { client } = setup(() => jsonResponse(403, { detail: "Missing permission: team.manage" }));

    const err = await client.get("/x").catch((e) => e);

    expect(err.code).toBeNull();
  });

  it("joins FastAPI validation messages into detail", async () => {
    const errors = [
      { loc: ["body", "password"], msg: "too short" },
      { loc: ["body", "email"], msg: "invalid email" },
    ];
    const { client } = setup(() => jsonResponse(422, { detail: errors }));

    const err = await client.post("/auth/signup", {}).catch((e) => e);

    expect(err.status).toBe(422);
    expect(err.detail).toBe("too short; invalid email");
    expect(err.errors).toEqual(errors);
  });

  it("returns null for 204 responses", async () => {
    const { client } = setup(() => new Response(null, { status: 204 }));
    expect(await client.del("/x")).toBeNull();
  });

  it("keeps the session when the refresh call hits a network error", async () => {
    const { client, onAuthFailure, getToken } = setup((url) => {
      if (url === `${BASE}/auth/refresh`) throw new TypeError("network down");
      return jsonResponse(401, { detail: "expired" });
    });

    await expect(client.get("/me")).rejects.toThrow("network down");
    expect(onAuthFailure).not.toHaveBeenCalled();
    expect(getToken()).toBe("old");
  });

  it("keeps the session when the refresh call returns 5xx", async () => {
    const { client, onAuthFailure, getToken } = setup((url) => {
      if (url === `${BASE}/auth/refresh`) {
        return jsonResponse(503, { detail: "unavailable" });
      }
      return jsonResponse(401, { detail: "expired" });
    });

    await expect(client.get("/me")).rejects.toMatchObject({ status: 503 });
    expect(onAuthFailure).not.toHaveBeenCalled();
    expect(getToken()).toBe("old");
  });

  it("clears the session when refresh returns 401 or 403", async () => {
    for (const status of [401, 403]) {
      const { client, onAuthFailure, getToken } = setup((url) => {
        if (url === `${BASE}/auth/refresh`) {
          return jsonResponse(status, { detail: "nope" });
        }
        return jsonResponse(401, { detail: "expired" });
      });

      await expect(client.get("/me")).rejects.toMatchObject({ status: 401 });
      expect(onAuthFailure).toHaveBeenCalledTimes(1);
      expect(getToken()).toBeNull();
    }
  });
});

describe("api.upload", () => {
  class FakeXhr {
    static instances = [];
    constructor() {
      this.headers = {};
      this.upload = {};
      FakeXhr.instances.push(this);
    }
    open(method, url) {
      this.method = method;
      this.url = url;
    }
    setRequestHeader(k, v) {
      this.headers[k] = v;
    }
    abort() {
      this.onabort?.();
    }
    send(body) {
      this.body = body;
      FakeXhr.script.shift()(this);
    }
  }
  const finish = (xhr, status, body) => {
    xhr.status = status;
    xhr.responseText = body === undefined ? "" : JSON.stringify(body);
    xhr.onload();
  };

  function setupUpload(refreshOk = true) {
    FakeXhr.instances = [];
    FakeXhr.script = [];
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const fetchMock = vi.fn(async () =>
      refreshOk ? jsonResponse(200, { access_token: "new" }) : jsonResponse(401, { detail: "no" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    let token = "old";
    const client = createApiClient({
      baseUrl: BASE,
      getAccessToken: () => token,
      setAccessToken: (t) => {
        token = t;
      },
    });
    return { client, fetchMock };
  }

  it("refreshes once on 401, resends with the new bearer, no Content-Type, reports progress", async () => {
    const { client, fetchMock } = setupUpload();
    FakeXhr.script.push((x) => finish(x, 401, { detail: "expired" }));
    FakeXhr.script.push((x) => {
      x.upload.onprogress({ lengthComputable: true, loaded: 5, total: 10 });
      finish(x, 201, { id: "d1" });
    });
    const onProgress = vi.fn();
    const form = new FormData();
    form.append("file", new Blob(["hi"]), "a.md");
    const out = await client.upload("/orgs/o/kbs/k/documents", form, { onProgress });
    expect(out).toEqual({ id: "d1" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/auth/refresh`);
    const [first, second] = FakeXhr.instances;
    expect(first.headers.Authorization).toBe("Bearer old");
    expect(second.headers.Authorization).toBe("Bearer new");
    expect(second.headers["Content-Type"]).toBeUndefined();
    expect(second.body).toBe(form);
    expect(onProgress).toHaveBeenCalledWith(0.5);
  });

  it("rejects with ApiError carrying status, detail and code", async () => {
    const { client } = setupUpload();
    FakeXhr.script.push((x) => finish(x, 409, { detail: "dup", code: "duplicate_document" }));
    await expect(client.upload("/x", new FormData())).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      detail: "dup",
      code: "duplicate_document",
    });
  });

  it("rejects with ApiError on a network error and on abort", async () => {
    const { client } = setupUpload();
    FakeXhr.script.push((x) => x.onerror());
    await expect(client.upload("/x", new FormData())).rejects.toMatchObject({ status: 0 });
    const ctrl = new AbortController();
    FakeXhr.script.push(() => ctrl.abort());
    await expect(client.upload("/x", new FormData(), { signal: ctrl.signal })).rejects.toMatchObject({
      status: 0,
      detail: "Upload cancelled",
    });
  });

  it("blob returns the body as a Blob and throws ApiError on failure", async () => {
    const { client } = setup((url) =>
      Promise.resolve(url.endsWith("/ok") ? new Response("abc", { status: 200 }) : jsonResponse(404, { detail: "Not found" })),
    );
    expect(await (await client.blob("/ok")).text()).toBe("abc");
    await expect(client.blob("/missing")).rejects.toMatchObject({ status: 404, detail: "Not found" });
  });
});
