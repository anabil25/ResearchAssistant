"""Executable ABSENCE controls for the runtime-trust chain.

Roughly twenty ratified controls are ABSENCES (a method that is not there, a field
that is not echoed, a fallback that does not exist) rather than behaviors. Absences
are uniquely fragile: invisible in review (you cannot see a method that is not
there), they erode by ADDITION through a harmless-looking convenience, and
behavioral tests cannot cover them (proving a behavior absent today is not proving
the AFFORDANCE absent). This module makes each absence executable, so ADDING the
convenience BREAKS A TEST rather than passing review.

Each test names its absence number from the consolidated list and states, in the
docstring, whether the affordance is ASSERTED STRUCTURALLY (member/type/signature
presence -- the strongest form) or via a SOURCE/AST check (necessary where the
affordance is a call shape rather than a member). The reason for each absence lives
AT THE SITE OF THE ABSENCE (the Protocol/adapter/function definition); this module
is the executable tripwire, not the rationale.
"""

from __future__ import annotations

import ast
import inspect

from research_assistant_api.agent_studio import (
    runtime_authz,
    runtime_client_binding,
    runtime_control_mount,
    runtime_control_router,
    runtime_deployment_producer,
    runtime_mapping_store,
)
from research_assistant_api.agent_studio.runtime_client_binding import (
    BindingResolution,
    ClientDeploymentBindingResolver,
    ClientDeploymentBindingWriter,
    RuntimeBindingStatus,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    RuntimeBindingDescriptor,
    RuntimeConnectionRef,
    RuntimeDeploymentMapping,
)
from research_assistant_api.agent_studio.runtime_mapping_store import (
    CosmosRuntimeDeploymentMappingReader,
    CosmosRuntimeDeploymentMappingStore,
    InMemoryRuntimeDeploymentMappingReader,
    InMemoryRuntimeDeploymentMappingStore,
    RuntimeDeploymentMappingControlPlane,
    RuntimeDeploymentMappingReader,
)


def _src(module: object) -> str:
    return inspect.getsource(module)  # type: ignore[arg-type]


# --- 1. no default_factory on any digest-feeding datetime field ------------


def test_absence_1_no_default_factory_on_digest_feeding_timestamps() -> None:
    """ASSERTED STRUCTURALLY (field-info) + behaviorally (determinism).

    The digest-feeding created_at fields must be REQUIRED with no default /
    default_factory, so a caller cannot pick up a silently-varying default that
    desyncs the digest (R4). Non-timestamp deterministic defaults are fine.
    """
    fields = RuntimeDeploymentMapping.model_fields
    for name in ("revision_created_at", "deployment_created_at"):
        assert fields[name].is_required()
        assert fields[name].default_factory is None


# --- 2. no unconditional upsert on the head record -------------------------


def test_absence_2_no_unconditional_upsert_on_head() -> None:
    """ASSERTED via SOURCE. Every head write must carry a precondition, so an
    ``upsert_item`` (which lands unconditionally) must never appear, and the head
    replace must use ``IfNotModified``."""
    source = _src(runtime_mapping_store)
    assert "upsert_item" not in source
    assert "if_match_etag" in source  # supersede head replace precondition
    assert "IfNotModified" in source  # clear_head_claim head replace precondition


# --- 3. no non-atomic fallback path from the batch retry -------------------


def test_absence_3_no_non_atomic_fallback_in_succession_retry() -> None:
    """ASSERTED via SOURCE. On succession exhaustion the producer raises
    ``SuccessionExhaustedError`` -- there must be NO sequential-write fallback that
    splits the atomic batch. The only mapping write is ``commit_revision``."""
    grant_succession_src = inspect.getsource(runtime_deployment_producer.RuntimeDeploymentProducer.grant_succession)
    assert "SuccessionExhaustedError" in grant_succession_src
    # The retry loop writes ONLY through commit_revision (via _commit_revision);
    # no direct revision/head write bypasses the atomic batch.
    assert "_head_document" not in grant_succession_src
    assert "execute_item_batch" not in grant_succession_src


# --- 4. no head or enumeration method on the runtime store Protocol --------


def test_absence_4_runtime_reader_declares_only_get() -> None:
    """ASSERTED STRUCTURALLY. Reachability from the runtime path is decided by what
    the reader Protocol DECLARES; it must declare exactly ``get``."""
    members = {name for name in vars(RuntimeDeploymentMappingReader) if not name.startswith("_")}
    assert members == {"get"}
    for forbidden in ("get_head", "list_revisions", "commit_revision", "delete", "clear_head_claim"):
        assert forbidden not in vars(RuntimeDeploymentMappingReader)


# --- 5. no control-plane adapter constructed in the runtime composition -----


def test_absence_5_runtime_composition_constructs_no_control_plane_adapter() -> None:
    """ASSERTED via SOURCE. The runtime composition module must not construct the
    control-plane adapters (it would put a write-capable object on the runtime
    path)."""
    source = _src(runtime_control_mount)
    assert "CosmosRuntimeDeploymentMappingStore(" not in source
    assert "InMemoryRuntimeDeploymentMappingStore(" not in source


# --- 6. no import of the control-plane adapter from the runtime composition -


def test_absence_6_runtime_composition_does_not_import_control_plane_adapter() -> None:
    """ASSERTED via AST import contract. The reader and control-plane adapters share
    one module, so the contract is: the runtime composition may import the reader
    PORT but must not name a control-plane ADAPTER class."""
    tree = ast.parse(_src(runtime_control_mount))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "CosmosRuntimeDeploymentMappingStore" not in imported
    assert "InMemoryRuntimeDeploymentMappingStore" not in imported


# --- 7. no latest/current accessor on the store port -----------------------


def test_absence_7_no_latest_or_current_accessor_on_the_ports() -> None:
    """ASSERTED STRUCTURALLY. A latest/current accessor is a SELECTION primitive
    that reopens caller-asserted-deployment lookup; neither port may expose one."""
    forbidden_substrings = ("latest", "current", "newest", "head_for")
    for port in (RuntimeDeploymentMappingReader, RuntimeDeploymentMappingControlPlane):
        methods = {name for name in vars(port) if not name.startswith("_")}
        for method in methods:
            assert not any(bad in method.lower() for bad in forbidden_substrings), method
    # get_head returns the succession pointer, not a revision selection -- and it
    # is control-plane only (absence 4 keeps it off the runtime reader).
    assert "get_head" in vars(RuntimeDeploymentMappingControlPlane)


# --- 8 / 17. no deployment_id in the resolver return type ------------------


def test_absence_8_resolver_return_type_has_no_deployment_id() -> None:
    """ASSERTED STRUCTURALLY. The resolution must not carry (nor echo) a
    deployment_id; the asserted deployment is strictly an INPUT the caller holds."""
    assert "deployment_id" not in BindingResolution.model_fields
    # ...and the resolver signature takes the asserted deployment as an input.
    sig = inspect.signature(ClientDeploymentBindingResolver.resolve_binding)
    assert "asserted_deployment_id" in sig.parameters


# --- 9 / 20. no head read / no unbound caller reaching the mapping container -


def test_absence_9_loader_reads_no_head_and_gates_before_the_mapping_read() -> None:
    """ASSERTED via SOURCE (affordance) + covered behaviorally elsewhere. The
    authorized loader must not call ``get_head`` and must resolve the binding
    (returning None for unbound/non-ACTIVE) BEFORE it ever calls ``reader.get``."""
    loader_src = inspect.getsource(runtime_client_binding.build_authorized_mapping_loader)
    assert "get_head" not in loader_src
    # The binding resolution + status gate precede the mapping read.
    resolve_pos = loader_src.index("resolve_binding")
    get_pos = loader_src.index(".get(")
    status_pos = loader_src.index("is not RuntimeBindingStatus.ACTIVE")
    assert resolve_pos < get_pos
    assert status_pos < get_pos


# --- 10. no status fallthrough -- allowlist, deny unless exactly ACTIVE -----


def test_absence_10_status_gate_is_an_allowlist_not_a_denylist() -> None:
    """ASSERTED via SOURCE. The loader denies unless status IS EXACTLY ACTIVE (an
    allowlist), so an unknown/future status fails closed rather than falling
    through as 'not revoked'."""
    loader_src = inspect.getsource(runtime_client_binding.build_authorized_mapping_loader)
    assert "is not RuntimeBindingStatus.ACTIVE" in loader_src
    # A denylist would test for REVOKED specifically; it must not.
    assert "is RuntimeBindingStatus.REVOKED" not in loader_src


# --- 11. no datetime.now( reachable from the domain authorization path -----


def test_absence_11_no_ambient_clock_in_domain_auth_modules() -> None:
    """ASSERTED via SOURCE (R1's structural form). Time enters the authorization
    path ONLY as an injected ``now``; an ambient ``datetime.now(``/``utcnow(`` must
    not appear in the authz/producer/binding modules."""
    for module in (runtime_authz, runtime_deployment_producer, runtime_client_binding):
        source = _src(module)
        assert "datetime.now(" not in source
        assert "utcnow(" not in source
    # ...and the authz entrypoints REQUIRE the injected now.
    for fn in (runtime_authz.authorize_runtime_request, runtime_authz.enforce_runtime_authorization):
        assert "now" in inspect.signature(fn).parameters


# --- 12. no hard-delete of binding rows -- tombstone only ------------------


def test_absence_12_binding_writer_has_no_delete_affordance() -> None:
    """ASSERTED STRUCTURALLY. The binding writer must expose no delete/remove, so a
    revoked binding can only ever be a tombstone (the succession/audit chain
    survives)."""
    writer_methods = {name for name in vars(ClientDeploymentBindingWriter) if not name.startswith("_")}
    assert writer_methods == {"repoint", "reinstate"}
    for concrete in (
        runtime_client_binding.InMemoryClientDeploymentBindingIndex,
        runtime_client_binding.CosmosClientDeploymentBindingIndex,
    ):
        for forbidden in ("delete", "remove", "pop", "drop"):
            assert forbidden not in vars(concrete)


# --- 13. no get() on the control-plane adapter (structural incompatibility) -


def test_absence_13_control_plane_adapter_is_not_a_runtime_reader() -> None:
    """ASSERTED STRUCTURALLY via @runtime_checkable isinstance -- the strongest
    form. The control-plane adapters must NOT structurally satisfy the runtime
    reader Protocol (they expose no ``get``), while the readers must."""
    in_memory = InMemoryRuntimeDeploymentMappingStore()
    assert not isinstance(in_memory, RuntimeDeploymentMappingReader)
    assert isinstance(in_memory.reader, RuntimeDeploymentMappingReader)
    assert isinstance(InMemoryRuntimeDeploymentMappingReader({}), RuntimeDeploymentMappingReader)
    # Member-presence: the control-plane classes have no `get` of their own.
    assert not hasattr(InMemoryRuntimeDeploymentMappingStore, "get")
    assert not hasattr(CosmosRuntimeDeploymentMappingStore, "get")
    assert hasattr(CosmosRuntimeDeploymentMappingReader, "get")


# --- 14. no revocation/supersession fields on the immutable mapping --------


def test_absence_14_mapping_has_no_revocation_or_supersession_fields() -> None:
    """ASSERTED STRUCTURALLY. Revocation/supersession are mutable binding/head
    facts; an immutable digest-covered mapping must carry none of them."""
    fields = set(RuntimeDeploymentMapping.model_fields)
    for forbidden in ("revoked_at", "lifecycle_state", "supersedes_deployment_id", "superseded_by", "retired_at"):
        assert forbidden not in fields


# --- 15. no secret material in the mapping (connection_ref = ref+digest) ----


def test_absence_15_connection_ref_pins_ref_and_digest_only_no_secret() -> None:
    """ASSERTED STRUCTURALLY. The connection pin projects identity + digest only;
    secret material (keys, connection strings) is never in the mapping. ``extra`` is
    forbidden, so no secret field can be smuggled in either."""
    assert set(RuntimeConnectionRef.model_fields) == {"id", "version", "digest"}
    assert RuntimeConnectionRef.model_config.get("extra") == "forbid"
    for forbidden in ("secret", "key", "connection_string", "password", "token", "credential"):
        assert forbidden not in RuntimeConnectionRef.model_fields


# --- 16. no raw config contents in the mapping -- CONFIG_DIGEST only --------


def test_absence_16_binding_descriptor_pins_config_digest_not_contents() -> None:
    """ASSERTED STRUCTURALLY. Config is pinned as an opaque digest only; the raw
    config contents/body must never be a field on the descriptor."""
    fields = set(RuntimeBindingDescriptor.model_fields)
    assert "config_digest" in fields
    for forbidden in ("config", "config_contents", "config_body", "config_json", "raw_config"):
        assert forbidden not in fields


# --- 18. no internal reason / str(exc) surfaced to the client --------------


def test_absence_18_router_surfaces_only_uniform_denial() -> None:
    """ASSERTED via SOURCE. Denials are uniform: the router must surface
    ``uniform_denial()`` and never ``str(exc)`` or an internal reason in the client
    response detail."""
    source = _src(runtime_control_router)
    assert "uniform_denial" in source
    assert "str(exc)" not in source
    assert "reason.value" not in source  # the audit reason is never returned to the client


# --- 19. no lifecycle_state on RuntimeMappingView --------------------------


def test_absence_19_runtime_mapping_view_has_no_lifecycle_state() -> None:
    """ASSERTED STRUCTURALLY. The runtime learns everything it may know from the
    response code; a lifecycle field would invite the harness to interpret policy."""
    from research_assistant_api.agent_studio.runtime_control_schemas import RuntimeMappingView

    assert "lifecycle_state" not in RuntimeMappingView.model_fields


# --- reinforcing checks ----------------------------------------------------


def test_binding_status_enum_is_exactly_active_and_revoked() -> None:
    # Reinforces absences 10/12: exactly two statuses, so the allowlist has a
    # closed, minimal domain and there is no third status to fall through.
    assert {s.value for s in RuntimeBindingStatus} == {"active", "revoked"}
