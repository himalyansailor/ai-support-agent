"""M2: every domain error response is {"detail": msg, "code": "<snake_case>"}."""

import re
from uuid import uuid4

from supportapi.errors import DOMAIN_ERRORS

_SNAKE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


def test_every_domain_error_has_a_unique_snake_case_code():
    codes = [code for _status, code in DOMAIN_ERRORS.values()]
    assert all(_SNAKE.fullmatch(c) for c in codes), codes
    assert len(codes) == len(set(codes))


def test_required_codes_are_present():
    codes = {code for _status, code in DOMAIN_ERRORS.values()}
    assert {
        "last_owner",
        "role_in_use",
        "role_name_taken",
        "already_member",
        "invite_account_exists",
        "invite_invalid",
        "permission_escalation",
        "email_taken",
        "invalid_credentials",
        "weak_password",
        "not_found",
    } <= codes


async def test_error_bodies_carry_code(client, auth_headers):
    creds = {"email": "a@x.com", "password": "correct-horse-1", "name": "A"}
    r = await client.post("/auth/signup", json=creds)
    assert r.status_code == 409 and r.json() == {"detail": "Email is already registered", "code": "email_taken"}
    r = await client.post("/auth/login", json={"email": "a@x.com", "password": "wrong-password-1"})
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"
    r = await client.post("/auth/signup", json={**creds, "email": "b@x.com", "password": "short"})
    assert r.status_code == 422 and r.json()["code"] == "weak_password"
    r = await client.get(f"/orgs/{uuid4()}", headers=auth_headers)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = await client.post("/invites/preview", json={"token": "nope"})
    assert r.status_code == 410 and r.json()["code"] == "invite_invalid"
