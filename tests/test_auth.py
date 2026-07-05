import base64
from datetime import UTC, datetime

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from combomaker.core.clock import FakeClock
from combomaker.exchange.auth import (
    ENV_API_KEY_ID,
    ENV_PRIVATE_KEY_PEM,
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    Credentials,
    CredentialsError,
    RequestSigner,
)


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def signer(key: rsa.RSAPrivateKey) -> RequestSigner:
    clock = FakeClock(start=datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC))
    return RequestSigner(Credentials(api_key_id="test-key-id", private_key=key), clock)


class TestSigning:
    def test_header_names_exact(self, signer: RequestSigner) -> None:
        headers = signer.headers("GET", "/trade-api/v2/portfolio/balance")
        assert set(headers) == {HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP}
        assert headers[HEADER_KEY] == "test-key-id"

    def test_timestamp_is_milliseconds(self, signer: RequestSigner) -> None:
        headers = signer.headers("GET", "/trade-api/v2/portfolio/balance")
        expected_ms = int(datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
        assert headers[HEADER_TIMESTAMP] == str(expected_ms)
        assert len(headers[HEADER_TIMESTAMP]) == 13  # ms not seconds

    def test_signature_verifies_with_pss_digest_salt(
        self, signer: RequestSigner, key: rsa.RSAPrivateKey
    ) -> None:
        path = "/trade-api/v2/portfolio/balance"
        headers = signer.headers("get", path)  # method must be uppercased
        message = f"{headers[HEADER_TIMESTAMP]}GET{path}".encode()
        key.public_key().verify(
            base64.b64decode(headers[HEADER_SIGNATURE]),
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def test_query_params_stripped_from_signed_message(
        self, signer: RequestSigner, key: rsa.RSAPrivateKey
    ) -> None:
        headers = signer.headers("GET", "/trade-api/v2/portfolio/orders?limit=5")
        message = f"{headers[HEADER_TIMESTAMP]}GET/trade-api/v2/portfolio/orders".encode()
        key.public_key().verify(
            base64.b64decode(headers[HEADER_SIGNATURE]),
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def test_wrong_message_fails_verification(
        self, signer: RequestSigner, key: rsa.RSAPrivateKey
    ) -> None:
        headers = signer.headers("GET", "/trade-api/v2/portfolio/balance")
        # Signing without the /trade-api/v2 prefix is the classic mistake.
        wrong = f"{headers[HEADER_TIMESTAMP]}GET/portfolio/balance".encode()
        with pytest.raises(InvalidSignature):
            key.public_key().verify(
                base64.b64decode(headers[HEADER_SIGNATURE]),
                wrong,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256(),
            )

    def test_relative_path_rejected(self, signer: RequestSigner) -> None:
        with pytest.raises(ValueError):
            signer.headers("GET", "portfolio/balance")


class TestCredentials:
    def test_from_env_missing_key_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_API_KEY_ID, raising=False)
        with pytest.raises(CredentialsError):
            Credentials.from_env()

    def test_from_env_pem(self, monkeypatch: pytest.MonkeyPatch, key: rsa.RSAPrivateKey) -> None:
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        monkeypatch.setenv(ENV_API_KEY_ID, "abc")
        monkeypatch.setenv(ENV_PRIVATE_KEY_PEM, pem)
        creds = Credentials.from_env()
        assert creds.api_key_id == "abc"

    def test_repr_never_leaks_key(self, key: rsa.RSAPrivateKey) -> None:
        creds = Credentials(api_key_id="abcdefgh-1234", private_key=key)
        assert "RSAPrivateKey" not in repr(creds)
        assert "BEGIN" not in repr(creds)
        assert "abcdefgh" in repr(creds)
