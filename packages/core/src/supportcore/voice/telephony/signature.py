"""Twilio's request signature: base64(HMAC-SHA1(auth_token, url + each param's key and value in key order)).

Callers with repeated form keys must flatten them first (Twilio sorts and concatenates every value).
"""

import base64
import hashlib
import hmac
from collections.abc import Mapping


def twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid_signature(auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool:
    return hmac.compare_digest(twilio_signature(auth_token, url, params).encode(), signature.encode())
