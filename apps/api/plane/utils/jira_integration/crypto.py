# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import base64
import hashlib

# Third party imports
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _credential_key():
    configured = str(getattr(settings, "JIRA_CREDENTIAL_ENCRYPTION_KEY", "") or "").strip()
    if configured:
        return configured.encode("utf-8")
    # A separate production key is preferred. Derivation keeps local installs
    # working while preserving the same at-rest boundary as the application key.
    digest = hashlib.sha256(f"plane-jira:{settings.SECRET_KEY}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_credential(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Credential must be a non-empty string")
    return Fernet(_credential_key()).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_credential(value):
    if not value:
        raise ValueError("Encrypted credential is missing")
    try:
        return Fernet(_credential_key()).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise ValueError("Jira credential cannot be decrypted with the configured key") from exc
