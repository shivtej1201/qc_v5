"""Who is calling (blueprint §12). QC6_AUTH chooses the mode:

  dev     (default) the member named in the X-QC-User header, else the workspace owner. Refused when
          QC6_ENV=production.
  proxy   behind an authenticating proxy such as oauth2-proxy (free) in front of Keycloak, Google Workspace or
          Azure AD: the proxy signs people in and passes their e-mail in a header (QC6_AUTH_HEADER, default
          X-Forwarded-Email). The header is trusted only from QC6_TRUSTED_PROXIES (default 127.0.0.1,::1).
  oidc    for API clients: `Authorization: Bearer <JWT>` from your OIDC provider (Keycloak …), verified with the
          provider's JWKS (RS256), issuer (QC6_OIDC_ISSUER), audience (QC6_OIDC_AUDIENCE) and expiry. The live
          stream (EventSource can't send headers) may pass the same token as ?access_token=.

In every mode the e-mail must belong to a workspace member; nobody is created automatically.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx

_jwks: Dict[str, Any] = {"at": 0.0, "keys": {}}
_fetch = None                              # tests replace with an async function returning the JWKS dict


class AuthError(Exception):
    def __init__(self, status: int, title: str, detail: str = ""):
        super().__init__(title)
        self.status, self.title, self.detail = status, title, detail


def mode() -> str:
    m = os.environ.get("QC6_AUTH", "dev").lower()
    if m == "dev" and os.environ.get("QC6_ENV", "").lower() == "production":
        raise AuthError(500, "Sign-in is not configured", "QC6_AUTH=dev is not allowed with QC6_ENV=production.")
    if m not in ("dev", "proxy", "oidc"):
        raise AuthError(500, "Sign-in is not configured", f"Unknown QC6_AUTH mode {m!r}.")
    return m


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


async def _get_jwks() -> Dict[str, Any]:
    if time.time() - _jwks["at"] < 600 and _jwks["keys"]:
        return _jwks["keys"]
    if _fetch is not None:
        data = await _fetch()
    else:
        url = os.environ.get("QC6_OIDC_JWKS_URL", "")
        if not url.startswith("https://"):
            raise AuthError(500, "Sign-in is not configured", "QC6_OIDC_JWKS_URL must be an https URL.")
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
    _jwks["keys"] = {k["kid"]: k for k in data.get("keys", []) if k.get("kty") == "RSA" and k.get("kid")}
    _jwks["at"] = time.time()
    return _jwks["keys"]


async def verify_jwt(token: str) -> Dict[str, Any]:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    try:
        h64, p64, s64 = token.split(".")
        header, claims, sig = json.loads(_b64(h64)), json.loads(_b64(p64)), _b64(s64)
    except (ValueError, json.JSONDecodeError):
        raise AuthError(401, "Sign in again", "The token is malformed.")
    if header.get("alg") != "RS256":
        raise AuthError(401, "Sign in again", "Only RS256 tokens are accepted.")
    keys = await _get_jwks()
    jwk = keys.get(header.get("kid"))
    if jwk is None:                                   # key rotation: refresh once
        _jwks["at"] = 0
        jwk = (await _get_jwks()).get(header.get("kid"))
    if jwk is None:
        raise AuthError(401, "Sign in again", "The token was signed with an unknown key.")
    pub = rsa.RSAPublicNumbers(int.from_bytes(_b64(jwk["e"]), "big"), int.from_bytes(_b64(jwk["n"]), "big")).public_key()
    try:
        pub.verify(sig, f"{h64}.{p64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise AuthError(401, "Sign in again", "The token signature is not valid.")
    now = time.time()
    if claims.get("exp") is None or now > float(claims["exp"]) + 30:
        raise AuthError(401, "Sign in again", "The token has expired.")
    if claims.get("nbf") and now + 30 < float(claims["nbf"]):
        raise AuthError(401, "Sign in again", "The token is not valid yet.")
    iss = os.environ.get("QC6_OIDC_ISSUER")
    if iss and claims.get("iss") != iss:
        raise AuthError(401, "Sign in again", "The token comes from a different issuer.")
    aud = os.environ.get("QC6_OIDC_AUDIENCE")
    if aud:
        auds = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
        if aud not in auds and claims.get("azp") != aud:
            raise AuthError(401, "Sign in again", "The token is meant for a different application.")
    return claims


async def caller_email(headers: Dict[str, str], client_host: Optional[str], query: Dict[str, str]) -> Tuple[str, Optional[str]]:
    """(mode, email). email None in dev mode without a header (= workspace owner)."""
    m = mode()
    h = {k.lower(): v for k, v in headers.items()}
    if m == "dev":
        return m, h.get("x-qc-user")
    if m == "proxy":
        trusted = [x.strip() for x in os.environ.get("QC6_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if x.strip()]
        if client_host not in trusted:
            raise AuthError(401, "Sign in first", "Requests must come through the sign-in proxy.")
        email = h.get(os.environ.get("QC6_AUTH_HEADER", "X-Forwarded-Email").lower())
        if not email:
            raise AuthError(401, "Sign in first", "The sign-in proxy didn't pass an e-mail.")
        return m, email
    auth = h.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else query.get("access_token", "")
    if not token:
        raise AuthError(401, "Sign in first", "Send Authorization: Bearer <token>.")
    claims = await verify_jwt(token)
    email = claims.get("email") or claims.get("preferred_username")
    if not email or (claims.get("email_verified") is False):
        raise AuthError(403, "Your account has no verified e-mail")
    return m, email
