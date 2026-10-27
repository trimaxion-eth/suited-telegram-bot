"""One EVM keypair, enough to sign a suited.club sign-in message.

`suited.club` authenticates with the standard EIP-4361 flow: ask for a
challenge, sign the returned text as an EIP-191 `personal_sign` message, hand
the signature back. That is ~40 lines of secp256k1 plus one keccak — far less
than pulling in a full Ethereum SDK, and the only dependency is `coincurve`,
which is already installed on this box.

The key this module loads is a **reader identity**: it never holds funds and
every use of it here is a read (`/api/me`, `/api/lobby`, the table socket).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

try:  # coincurve is present here; the error message is the useful fallback
    from coincurve import PrivateKey, PublicKey
except ImportError as error:  # pragma: no cover
    raise SystemExit(
        "spectator mode needs coincurve (`pip install --user coincurve`)"
    ) from error


def _keccak(data: bytes) -> bytes:
    try:
        from Crypto.Hash import keccak

        digest = keccak.new(digest_bits=256)
        digest.update(data)
        return digest.digest()
    except ImportError:  # pragma: no cover
        from eth_utils import keccak as eth_keccak

        return eth_keccak(data)


def address_of(private_key: PrivateKey) -> str:
    public = private_key.public_key.format(compressed=False)[1:]
    return "0x" + _keccak(public)[-20:].hex()


def generate(path: Path) -> tuple[str, str]:
    """Create a fresh key at `path` (0600) and return (hex, address)."""
    key = PrivateKey(secrets.token_bytes(32))
    hexkey = key.to_hex()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        handle.write(hexkey + "\n")
    return hexkey, address_of(key)


def load(path: Path) -> tuple[PrivateKey, str]:
    hexkey = path.read_text().strip()
    if hexkey.startswith("0x"):
        hexkey = hexkey[2:]
    key = PrivateKey(bytes.fromhex(hexkey))
    return key, address_of(key)


def personal_sign(private_key: PrivateKey, message: str) -> str:
    """EIP-191 `personal_sign`: 65-byte r||s||v, v in {27,28}."""
    body = message.encode("utf-8")
    prefix = f"\x19Ethereum Signed Message:\n{len(body)}".encode()
    digest = _keccak(prefix + body)
    signature = private_key.sign_recoverable(digest, hasher=None)
    return "0x" + signature.hex()


def recover_address(message: str, signature_hex: str) -> str:
    """Only used by the self-check: prove the signature verifies to our address."""
    body = message.encode("utf-8")
    prefix = f"\x19Ethereum Signed Message:\n{len(body)}".encode()
    digest = _keccak(prefix + body)
    raw = bytes.fromhex(signature_hex.removeprefix("0x"))
    public = PublicKey.from_signature_and_message(raw, digest, hasher=None)
    return "0x" + _keccak(public.format(compressed=False)[1:])[-20:].hex()
