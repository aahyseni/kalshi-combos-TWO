"""Kalshi request signing (doc-verified: docs/api-notes/auth-env.md).

Signed message = ``timestamp_ms + UPPERCASE_METHOD + path`` with no separators,
where ``path`` is the full path from the host root **including** the
``/trade-api/v2`` (or ``/trade-api/ws/v2``) prefix and with query parameters
stripped. The body is never signed. Signature: RSA-PSS, SHA256, MGF1(SHA256),
salt length = digest length (NOT max), standard base64.

Secrets come only from the environment: ``KALSHI_API_KEY_ID`` plus either
``KALSHI_PRIVATE_KEY_PATH`` (path to PEM) or ``KALSHI_PRIVATE_KEY_PEM`` (the
PEM itself). Key material is never logged, never repr'd, never persisted.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from combomaker.core.clock import Clock

ENV_API_KEY_ID = "KALSHI_API_KEY_ID"
ENV_PRIVATE_KEY_PATH = "KALSHI_PRIVATE_KEY_PATH"
ENV_PRIVATE_KEY_PEM = "KALSHI_PRIVATE_KEY_PEM"

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"


class CredentialsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class Credentials:
    api_key_id: str
    private_key: rsa.RSAPrivateKey = field(hash=False, compare=False)

    def __repr__(self) -> str:  # never leak key material via repr/logging
        return f"Credentials(api_key_id={self.api_key_id[:8]}…)"

    @classmethod
    def from_env(cls) -> Credentials:
        return cls.from_env_names(ENV_API_KEY_ID, ENV_PRIVATE_KEY_PATH, ENV_PRIVATE_KEY_PEM)

    @classmethod
    def for_env(cls, env: str) -> Credentials:
        """Environment-scoped credentials. Kalshi keys are strictly
        per-environment (ground truth: demo 401s prod keys), so prod REQUIRES
        the KALSHI_PROD_* variables — no silent fallback to demo keys."""
        if env == "prod":
            return cls.from_env_names(
                "KALSHI_PROD_API_KEY_ID",
                "KALSHI_PROD_PRIVATE_KEY_PATH",
                "KALSHI_PROD_PRIVATE_KEY_PEM",
            )
        return cls.from_env()

    @classmethod
    def from_env_names(cls, key_id_env: str, path_env: str, pem_env: str) -> Credentials:
        """Load from custom env var names (e.g. the ground-truth harness's
        second, requester-side demo account: KALSHI_REQUESTER_*)."""
        api_key_id = os.environ.get(key_id_env, "").strip()
        if not api_key_id:
            raise CredentialsError(f"{key_id_env} is not set")

        pem = os.environ.get(pem_env, "")
        if not pem:
            key_path = os.environ.get(path_env, "").strip()
            if not key_path:
                raise CredentialsError(f"set {path_env} (path to PEM) or {pem_env}")
            try:
                with open(key_path, "rb") as f:
                    pem_bytes = f.read()
            except OSError as exc:
                raise CredentialsError(f"cannot read private key file: {exc}") from exc
        else:
            pem_bytes = pem.encode("utf-8")

        try:
            key = serialization.load_pem_private_key(pem_bytes, password=None)
        except (ValueError, TypeError) as exc:
            raise CredentialsError(f"invalid private key PEM: {exc}") from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise CredentialsError("private key is not RSA")
        return cls(api_key_id=api_key_id, private_key=key)


class RequestSigner:
    def __init__(self, credentials: Credentials, clock: Clock) -> None:
        self._credentials = credentials
        self._clock = clock

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Auth headers for a request. ``path`` must include the API prefix."""
        if not path.startswith("/"):
            raise ValueError(f"path must be absolute from host root, got {path!r}")
        timestamp = str(int(self._clock.now().timestamp() * 1000))
        message = f"{timestamp}{method.upper()}{path.split('?')[0]}".encode()
        signature = self._credentials.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            HEADER_KEY: self._credentials.api_key_id,
            HEADER_SIGNATURE: base64.b64encode(signature).decode("ascii"),
            HEADER_TIMESTAMP: timestamp,
        }
