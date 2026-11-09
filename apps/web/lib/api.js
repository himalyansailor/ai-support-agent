export class ApiError extends Error {
  // code: the backend's machine-readable error code (e.g. "invite_account_exists"), or null.
  constructor(status, detail, errors = null, code = null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.errors = errors;
    this.code = code;
  }
}

function parseCode(body) {
  const code = body && typeof body === "object" ? body.code : undefined;
  return typeof code === "string" ? code : null;
}

function parseDetail(status, body) {
  const fallback = `Request failed (${status})`;
  const detail = body && typeof body === "object" ? body.detail : undefined;
  if (typeof detail === "string") return { detail, errors: null };
  if (Array.isArray(detail)) {
    const msgs = detail.map((e) => e?.msg).filter(Boolean);
    return { detail: msgs.length ? msgs.join("; ") : fallback, errors: detail };
  }
  return { detail: fallback, errors: null };
}

async function readBody(res) {
  if (res.status === 204) return null;
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function createApiClient({
  baseUrl,
  getAccessToken,
  setAccessToken,
  onAuthFailure,
}) {
  let refreshInFlight = null;

  async function send(method, path, body) {
    const headers = {};
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(`${baseUrl}${path}`, {
      method,
      headers,
      credentials: "include",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  async function toResult(res, asBlob = false) {
    if (asBlob && res.ok) return res.blob();
    const data = await readBody(res);
    if (!res.ok) {
      const { detail, errors } = parseDetail(res.status, data);
      throw new ApiError(res.status, detail, errors, parseCode(data));
    }
    return data;
  }

  // Single-flight: refresh tokens rotate on every use and a replay outside the
  // backend's short grace window revokes the whole family, so concurrent callers
  // in this tab share one call (other tabs are covered by the grace window).
  function refresh() {
    if (!refreshInFlight) {
      refreshInFlight = (async () => {
        const data = await toResult(await send("POST", "/auth/refresh"));
        setAccessToken(data.access_token);
        return data;
      })().finally(() => {
        refreshInFlight = null;
      });
    }
    return refreshInFlight;
  }

  // Multipart upload over XHR (fetch has no upload progress). Same auth header and
  // cookie handling as send(); resolves to a Response so toResult can be shared.
  function sendUpload(path, formData, onProgress, signal) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${baseUrl}${path}`);
      xhr.withCredentials = true;
      const token = getAccessToken();
      // No Content-Type: the browser sets the multipart boundary itself.
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      if (onProgress && xhr.upload) {
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) onProgress(e.loaded / e.total);
        };
      }
      xhr.onerror = () => reject(new ApiError(0, "Network error", null, null));
      xhr.onabort = () => reject(new ApiError(0, "Upload cancelled", null, null));
      xhr.onload = () => {
        try {
          resolve(
            new Response(xhr.status === 204 ? null : xhr.responseText, { status: xhr.status }),
          );
        } catch {
          reject(new ApiError(xhr.status, `Request failed (${xhr.status})`, null, null));
        }
      };
      if (signal) {
        if (signal.aborted) return reject(new ApiError(0, "Upload cancelled", null, null));
        signal.addEventListener("abort", () => xhr.abort(), { once: true });
      }
      xhr.send(formData);
    });
  }

  async function request(method, path, body, upload, asBlob = false) {
    const doSend = () =>
      upload ? sendUpload(path, body, upload.onProgress, upload.signal) : send(method, path, body);
    let res = await doSend();
    // Auth endpoints return 401 for bad credentials; refreshing can't help.
    if (res.status === 401 && !path.startsWith("/auth/")) {
      try {
        await refresh();
      } catch (err) {
        // Only a definitive auth rejection ends the session; network errors
        // and 5xx are transient and must not log the user out.
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          setAccessToken(null);
          onAuthFailure?.();
          return toResult(res, asBlob);
        }
        throw err;
      }
      res = await doSend();
    }
    return toResult(res, asBlob);
  }

  return {
    get: (path) => request("GET", path),
    // GET resolving to a Blob (downloads); errors are ApiErrors as for get.
    blob: (path) => request("GET", path, undefined, undefined, true),
    post: (path, body) => request("POST", path, body),
    put: (path, body) => request("PUT", path, body),
    patch: (path, body) => request("PATCH", path, body),
    del: (path) => request("DELETE", path),
    // formData is sent as multipart; onProgress receives a 0..1 fraction.
    // signal (AbortSignal) cancels the in-flight request.
    upload: (path, formData, { onProgress, signal } = {}) =>
      request("POST", path, formData, { onProgress, signal }),
    refresh,
  };
}
