"""Friday's Telegram Mini App (site/app/): one web page that shows a component, such as the form
(forms.py), and sends what the owner entered back encrypted to this server.

The bot opens it from a reply-keyboard button. The component's spec rides in the URL fragment,
which browsers never send to the web host: its kind, the request id, what to show and this
server's public key. The page encrypts the answer with a fresh AES-256-GCM key, wraps that key
with RSA-OAEP-SHA256 and sends {v, k, iv, ct} back through Telegram.WebApp.sendData, so Telegram
only sees ciphertext. The private key sits in the vault folder, which the agents can't read.

The page is static and the same for every Friday, so by default every install uses the one
published from this repo by GitHub Pages (.github/workflows/pages.yml). FRIDAY_APP_URL points it
at another copy; empty turns the Mini App off (requests are then answered by reply).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEFAULT_URL = "https://leejaywon.github.io/friday/app/"
KINDS = ("form", "planner")  # the components site/app/ can show
OAEP = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AppKey:
    """The server's RSA-3072 key pair for the Mini App, created on first use (0600, never replaced)."""

    def __init__(self, vault_dir: Path):
        self.path = vault_dir / "form_key.pem"

    def _private(self) -> rsa.RSAPrivateKey:
        if not self.path.exists():
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            try:  # O_EXCL: two processes starting at once keep the first key
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(pem)
        key = serialization.load_pem_private_key(self.path.read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return key

    def public_b64(self) -> str:
        """SubjectPublicKeyInfo (DER), base64url: what WebCrypto's importKey('spki') takes."""
        der = self._private().public_key().public_bytes(serialization.Encoding.DER,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo)
        return b64url(der)

    def decrypt(self, data: str) -> dict:
        """sendData's {v, k, iv, ct} -> {rid, kind, values}. Raises ValueError on anything malformed."""
        try:
            msg = json.loads(data)
            if msg.get("v") != 1:
                raise ValueError("unknown version")
            aes = self._private().decrypt(unb64url(msg["k"]), OAEP)
            plain = AESGCM(aes).decrypt(unb64url(msg["iv"]), unb64url(msg["ct"]), None)
            out = json.loads(plain)
            if not isinstance(out.get("rid"), int) or not isinstance(out.get("values"), dict):
                raise ValueError("bad payload")
            return out
        except ValueError:
            raise
        except Exception as e:  # wrong key, tampered data, missing keys; never echo the content
            raise ValueError(type(e).__name__) from None


def url(base: str, vault_dir: Path, rid: int, spec: dict) -> str:
    """The link that opens spec's component for request rid: base + '#' + base64url(JSON)."""
    assert spec.get("kind") in KINDS, spec.get("kind")
    full = {**spec, "v": 1, "rid": rid, "key": AppKey(vault_dir).public_b64()}
    return base + "#" + b64url(json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode())


def encrypt_like_the_page(public_b64: str, payload: dict) -> str:
    """What site/app/ sends, for tests."""
    pub = serialization.load_der_public_key(unb64url(public_b64))
    aes, iv = os.urandom(32), os.urandom(12)
    ct = AESGCM(aes).encrypt(iv, json.dumps(payload).encode(), None)
    return json.dumps({"v": 1, "k": b64url(pub.encrypt(aes, OAEP)), "iv": b64url(iv), "ct": b64url(ct)})  # type: ignore[union-attr]
