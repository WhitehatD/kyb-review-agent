"""Agent identity: short-lived, signed tokens that say who the agent is and what it may do.

The platform (this module, run by the harness) holds an Ed25519 private key and issues one token
per agent session. The MCP tool server only ever gets the public key, so it can check a token
but can never mint one. A token is bound to:

  sub    the agent identity, e.g. agent:kyb-reviewer@claude-haiku-5.5
  stage  its trust stage (shadow, suggest, execute, autonomous)
  scope  the tools that stage grants, space-separated
  case   the one case this session may touch
  exp    ten minutes after issue

The model never sees the token. The harness passes it to the server it starts for the session.
"""
import os
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYS = os.path.join(ROOT, "keys")
ISSUER, AUDIENCE, TTL = "kyb-platform", "kyb-mcp", 600


def _ensure_keys():
    priv = os.path.join(KEYS, "platform_ed25519.pem")
    pub = os.path.join(KEYS, "platform_ed25519.pub.pem")
    if not os.path.exists(priv):
        os.makedirs(KEYS, exist_ok=True)
        k = Ed25519PrivateKey.generate()
        open(priv, "wb").write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        open(pub, "wb").write(k.public_key().public_bytes(serialization.Encoding.PEM,
                                                          serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv, pub


def public_key_path():
    return _ensure_keys()[1]


def agent_id(model):
    return f"agent:kyb-reviewer@{model.split('/')[-1]}"


def issue(sub, stage, scopes, case_id, ttl=TTL):
    priv, _ = _ensure_keys()
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "stage": stage, "scope": " ".join(scopes),
              "case": case_id, "iat": now, "exp": now + ttl, "jti": uuid.uuid4().hex}
    return jwt.encode(claims, open(priv, "rb").read(), algorithm="EdDSA")


class Denied(Exception):
    pass


def verify(token, public_key_pem):
    """Returns the claims, or raises Denied with the reason."""
    try:
        return jwt.decode(token, public_key_pem, algorithms=["EdDSA"], audience=AUDIENCE, issuer=ISSUER,
                          options={"require": ["exp", "sub", "stage", "scope", "case"]})
    except jwt.ExpiredSignatureError:
        raise Denied("token expired")
    except jwt.InvalidTokenError as e:
        raise Denied(f"invalid token: {e}")
