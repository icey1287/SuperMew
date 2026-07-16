"""Request-owned destination capability issuer and verifier.

The model sees only the public ``evidence_id`` accepted by ``web_fetch``.  This
Module keeps the signing key and destination claims behind the execution Seam,
binds every capability to one Run security context, and forgets all capability
state when that request closes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass

from backend.guardrails.contracts import (
    DestinationCapability,
    ToolGuardrailRequest,
    destination_context_binding,
)


_ISSUER = "web-url-policy"
_POLICY_HASH = hashlib.sha256(
    json.dumps(
        {
            "issuer": _ISSUER,
            "network_policy": "restricted",
            "resource_scope": "public-web",
            "schema_version": 2,
            "tool_name": "web_fetch",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
).hexdigest()


def _canonical_claims(
    *,
    capability_id: str,
    context_binding: str,
    destination_hash: str,
    tool_name: str,
    network_policy: str,
    resource_scope: str,
) -> bytes:
    return json.dumps(
        {
            "capability_id": capability_id,
            "context_binding": context_binding,
            "destination_hash": destination_hash,
            "issuer": _ISSUER,
            "network_policy": network_policy,
            "policy_hash": _POLICY_HASH,
            "resource_scope": resource_scope,
            "schema_version": 2,
            "tool_name": tool_name,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _signature(secret: bytes, claims: bytes) -> str:
    digest = hmac.new(secret, claims, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True, repr=False)
class DestinationCapabilityBinding:
    user_id: str
    tenant_id: str
    thread_id: str
    run_id: str

    def __post_init__(self) -> None:
        # Validate the complete binding at construction so an invalid identity
        # cannot sit dormant until the first issue/verify call.
        self.fingerprint

    @property
    def fingerprint(self) -> str:
        return destination_context_binding(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            thread_id=self.thread_id,
            run_id=self.run_id,
        )

    def __repr__(self) -> str:
        return "DestinationCapabilityBinding(bound=True)"


class RunDestinationCapabilityAuthority:
    """Mint and verify opaque capabilities for exactly one request-owned Run."""

    def __init__(self, binding: DestinationCapabilityBinding) -> None:
        if not isinstance(binding, DestinationCapabilityBinding):
            raise TypeError("binding must be DestinationCapabilityBinding")
        self.binding = binding
        self._context_binding = binding.fingerprint
        self._secret = secrets.token_bytes(32)
        self._lock = threading.RLock()
        self._closed = False

    @property
    def policy_hash(self) -> str:
        return _POLICY_HASH

    def issue(
        self,
        destination: str,
        *,
        tool_name: str = "web_fetch",
        network_policy: str = "restricted",
        resource_scope: str = "public-web",
    ) -> DestinationCapability:
        """Issue claims internally; callers never receive the signing key or URL."""

        if not isinstance(destination, str) or not destination:
            raise ValueError("destination must be a non-empty canonical URL")
        encoded_destination = destination.encode("utf-8")
        if len(encoded_destination) > 16 * 1024 or b"\x00" in encoded_destination:
            raise ValueError("destination exceeds the capability limit")
        destination_hash = hashlib.sha256(encoded_destination).hexdigest()
        with self._lock:
            if self._closed:
                raise RuntimeError("destination capability authority is closed")
            nonce = secrets.token_bytes(32)
            capability_id = "destcap_" + hashlib.sha256(nonce).hexdigest()
            claims = _canonical_claims(
                capability_id=capability_id,
                context_binding=self._context_binding,
                destination_hash=destination_hash,
                tool_name=tool_name,
                network_policy=network_policy,
                resource_scope=resource_scope,
            )
            signature = _signature(self._secret, claims)
        return DestinationCapability(
            capability_id=capability_id,
            issuer=_ISSUER,
            policy_hash=_POLICY_HASH,
            context_binding=self._context_binding,
            destination_hash=destination_hash,
            tool_name=tool_name,
            network_policy=network_policy,
            resource_scope=resource_scope,
            signature=signature,
        )

    def verify(
        self,
        capability: DestinationCapability,
        *,
        request: ToolGuardrailRequest,
    ) -> bool:
        if not isinstance(capability, DestinationCapability) or not isinstance(
            request,
            ToolGuardrailRequest,
        ):
            return False
        claims = _canonical_claims(
            capability_id=capability.capability_id,
            context_binding=capability.context_binding,
            destination_hash=capability.destination_hash,
            tool_name=capability.tool_name,
            network_policy=capability.network_policy,
            resource_scope=capability.resource_scope,
        )
        with self._lock:
            if self._closed:
                return False
            expected_signature = _signature(self._secret, claims)
        return bool(
            capability.policy_hash == _POLICY_HASH
            and hmac.compare_digest(capability.context_binding, self._context_binding)
            and hmac.compare_digest(capability.signature, expected_signature)
            and request.user_id == self.binding.user_id
            and request.tenant_id == self.binding.tenant_id
            and request.thread_id == self.binding.thread_id
            and request.run_id == self.binding.run_id
            and request.tool_name == capability.tool_name
            and request.network_policy == capability.network_policy
            and request.resource_scope == capability.resource_scope
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._secret = b""

    def __repr__(self) -> str:
        with self._lock:
            state = "closed" if self._closed else "active"
        return f"RunDestinationCapabilityAuthority(state={state!r})"


__all__ = [
    "DestinationCapabilityBinding",
    "RunDestinationCapabilityAuthority",
]
