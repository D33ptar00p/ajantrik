"""Credential references: `env:NAME` or `keyring:service/name`. Values are read at the moment of use
and never written to config, job files, logs or agent prompts."""

from __future__ import annotations

import os

from ..util import FactoryError


def resolve(ref: str | None, what: str = "credential") -> str | None:
    if not ref:
        return None
    kind, _, name = ref.partition(":")
    if kind == "env":
        value = os.environ.get(name)
        if not value:
            raise FactoryError(f"{what}: environment variable {name} is not set")
        return value
    if kind == "keyring":
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as e:
            raise FactoryError(f"{what}: {ref} needs the keyring package (pip install keyring)") from e
        service, _, user = name.partition("/")
        value = keyring.get_password(service, user or "default")
        if not value:
            raise FactoryError(f"{what}: nothing stored in the keyring for {ref}")
        return value
    raise FactoryError(f"{what}: unsupported reference {ref!r} (use env:NAME or keyring:service/name)")
