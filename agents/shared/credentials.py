from __future__ import annotations

import os

from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential


def get_credential() -> TokenCredential:
    client_id = os.getenv("AZURE_CLIENT_ID")
    if client_id or os.getenv("IDENTITY_ENDPOINT") or os.getenv("MSI_ENDPOINT"):
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()
