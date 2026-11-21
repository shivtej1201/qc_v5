"""Sign-in modes: dev, trusted proxy, OIDC bearer (RS256 + JWKS)."""
import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from qc6 import auth, services


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(k, kid):
    n = k.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "n": b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")), "e": b64(n.e.to_bytes(3, "big"))}


def token(claims, key=KEY, kid="k1", alg="RS256"):
    h = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    p = b64(json.dumps(claims).encode())
    sig = key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{b64(sig)}"


@pytest.fixture(scope="module")
def client(migrated):
    import asyncpg
    from qc6.db.pool import _init

    async def seed():
        pool = await asyncpg.create_pool(migrated, min_size=1, max_size=2, init=_init)
        ws = await services.create_workspace(pool, "Auth Co", "owner@auth.test", "Olu Owner")
        await services.add_member(pool, ws["workspace_id"], "dev@auth.test", "Dee Dev", "editor")
        await pool.close()
    asyncio.run(seed())
    from qc6.api.app import app
    with TestClient(app) as c:
        yield c


def test_dev_mode(client, monkeypatch):
    monkeypatch.setenv("QC6_AUTH", "dev")
    assert client.get("/api/v2/me", headers={"X-QC-User": "dev@auth.test"}).json()["member"]["name"] == "Dee Dev"
    assert client.get("/api/v2/me", headers={"X-QC-User": "nobody@x.test"}).status_code == 403
    monkeypatch.setenv("QC6_ENV", "production")
    r = client.get("/api/v2/me")
    assert r.status_code == 500 and "not allowed with QC6_ENV=production" in r.json()["detail"]


def test_proxy_mode(client, monkeypatch):
    monkeypatch.setenv("QC6_AUTH", "proxy")
    monkeypatch.setenv("QC6_TRUSTED_PROXIES", "testclient")          # TestClient's peer address
    assert client.get("/api/v2/me", headers={"X-Forwarded-Email": "DEV@auth.test"}).json()["member"]["name"] == "Dee Dev"
    assert client.get("/api/v2/me").status_code == 401
    assert client.get("/api/v2/me", headers={"X-QC-User": "owner@auth.test"}).status_code == 401   # dev header ignored
    monkeypatch.setenv("QC6_TRUSTED_PROXIES", "10.0.0.5")
    r = client.get("/api/v2/me", headers={"X-Forwarded-Email": "dev@auth.test"})
    assert r.status_code == 401 and "through the sign-in proxy" in r.json()["detail"]


def test_oidc_mode(client, monkeypatch):
    monkeypatch.setenv("QC6_AUTH", "oidc")
    monkeypatch.setenv("QC6_OIDC_ISSUER", "https://sso.auth.test/realms/qc")
    monkeypatch.setenv("QC6_OIDC_AUDIENCE", "qc6")

    async def fetch():
        return {"keys": [jwk(KEY, "k1")]}
    monkeypatch.setattr(auth, "_fetch", fetch)
    auth._jwks.update({"at": 0.0, "keys": {}})
    now = int(time.time())
    good = {"iss": "https://sso.auth.test/realms/qc", "aud": "qc6", "exp": now + 300, "email": "dev@auth.test", "email_verified": True}
    H = lambda t: {"Authorization": f"Bearer {t}"}
    assert client.get("/api/v2/me", headers=H(token(good))).json()["member"]["name"] == "Dee Dev"
    assert client.get(f"/api/v2/me?access_token={token(good)}").status_code == 200
    cases = {
        "expired": token({**good, "exp": now - 120}),
        "issuer": token({**good, "iss": "https://evil.test"}),
        "audience": token({**good, "aud": "other-app"}),
        "forged": token(good, key=OTHER),
        "unknown kid": token(good, kid="k9"),
        "alg none": token(good)[: token(good).rfind(".")].replace(b64(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode()),
                                                                  b64(json.dumps({"alg": "none", "kid": "k1"}).encode())) + ".",
    }
    for name, t in cases.items():
        r = client.get("/api/v2/me", headers=H(t))
        assert r.status_code == 401, (name, r.status_code, r.text)
    r = client.get("/api/v2/me", headers=H(token({**good, "email": "stranger@auth.test"})))
    assert r.status_code == 403 and "Ask a workspace owner" in r.json()["detail"]
    assert client.get("/api/v2/me").status_code == 401


