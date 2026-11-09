from kb_helpers import create_kb, create_org, signup

UPLOAD_LIMIT = 2000 + 65536  # kb_settings.max_upload_bytes + multipart overhead
MIB = 1024 * 1024


def _upload_url(org_id="00000000-0000-0000-0000-000000000001", kb_id="00000000-0000-0000-0000-000000000002"):
    return f"/orgs/{org_id}/kbs/{kb_id}/documents"


async def test_oversize_content_length_is_413_before_auth_or_handler(kb_client):
    body = b"x" * (UPLOAD_LIMIT + 1)  # no Authorization header: must be 413, not 401 after parsing
    r = await kb_client.post(_upload_url(), content=body, headers={"content-type": "multipart/form-data; boundary=b"})
    assert (r.status_code, r.json()["code"]) == (413, "upload_too_large")


async def test_oversize_multipart_without_auth_is_413(kb_client):
    files = {"file": ("big.txt", b"a" * (UPLOAD_LIMIT + 10))}
    r = await kb_client.post(_upload_url(), files=files)
    assert (r.status_code, r.json()["code"]) == (413, "upload_too_large")


async def test_streamed_body_over_cap_is_413(kb_client):
    async def stream():  # valid multipart, no Content-Length (chunked)
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
        for _ in range(UPLOAD_LIMIT // 1000 + 5):
            yield b"a" * 1000
        yield b"\r\n--b--\r\n"

    r = await kb_client.post(_upload_url(), content=stream(), headers={"content-type": "multipart/form-data; boundary=b"})
    assert (r.status_code, r.json()["code"]) == (413, "upload_too_large")


async def test_streamed_json_over_default_cap_is_413(kb_client):
    async def stream():
        for _ in range(MIB // 1000 + 5):
            yield b" " * 1000

    r = await kb_client.post("/auth/login", content=stream(), headers={"content-type": "application/json"})
    assert (r.status_code, r.json()["code"]) == (413, "request_too_large")


async def test_json_over_default_cap_on_other_route_is_413(kb_client):
    r = await kb_client.post("/auth/login", json={"email": "a@x.com", "password": "p" * (MIB + 10)})
    assert (r.status_code, r.json()["code"]) == (413, "request_too_large")


async def test_normal_requests_unaffected(kb_client):
    headers = await signup(kb_client, "owner@x.com")  # JSON under 1 MiB
    org_id = await create_org(kb_client, headers)
    kb = await create_kb(kb_client, org_id, headers, description="d" * 1500)
    r = await kb_client.post(
        _upload_url(org_id, kb["id"]), files={"file": ("a.txt", b"a" * 2000)}, headers=headers
    )
    assert r.status_code == 201


async def test_413_carries_cors_headers(kb_client, kb_settings):
    origin = {"origin": kb_settings.web_origin}
    r = await kb_client.post(
        _upload_url(), content=b"x" * (UPLOAD_LIMIT + 1), headers={**origin, "content-type": "multipart/form-data; boundary=b"}
    )
    assert (r.status_code, r.json()["code"]) == (413, "upload_too_large")
    assert r.headers["access-control-allow-origin"] == kb_settings.web_origin

    async def stream():
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
        for _ in range(UPLOAD_LIMIT // 1000 + 5):
            yield b"a" * 1000

    r = await kb_client.post(
        _upload_url(), content=stream(), headers={**origin, "content-type": "multipart/form-data; boundary=b"}
    )
    assert r.status_code == 413
    assert r.headers["access-control-allow-origin"] == kb_settings.web_origin


async def test_cors_preflight_still_ok(kb_client, kb_settings):
    r = await kb_client.options(
        _upload_url(),
        headers={
            "origin": kb_settings.web_origin,
            "access-control-request-method": "POST",
            "access-control-request-headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == kb_settings.web_origin
