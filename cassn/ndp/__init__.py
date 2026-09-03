"""
National Data Platform staging for the CA-SSN source namespace.

Box holds the archival originals. NDP gets a per-deployment copy under
``ssn/ca/UC-Nature/source/<deployment_event_id>/<deployment_id>/``, described by
a ``manifest.json`` and a ``file_metadata.csv`` split out of the deployment
event's filed metadata. This package builds that description; nothing here
copies media, and nothing here writes to Box.

Named for the destination platform rather than the transport, matching
:mod:`cassn.soundhub`. OSDF is the storage federation and Pelican the transfer
tool; both are mechanisms and belong in modules inside this package.

Layers, bottom up:

* :mod:`cassn.ndp.manifest` — the ``cassn.source.deployment`` document, built
  from metadata rows and a curated lookup row. Pure: no filesystem, no clock,
  no network.
* :mod:`cassn.ndp.staging` — read one event's metadata, split it per
  deployment, render the output tree, and apply it idempotently.

Neither imports Qt.
"""

from cassn.ndp.manifest import (
    MANIFEST_TYPE,
    MANIFEST_VERSION,
    SNAPSHOT_VERSION,
    ManifestBuild,
    build_manifest,
    content_digest,
)
from cassn.ndp.staging import (
    ApplyResult,
    MetadataDocument,
    NdpStagingError,
    PlannedDeployment,
    StagingPlan,
    apply_plan,
    plan_event,
    read_event_documents,
    read_event_recorded_by,
)
from cassn.ndp.submission import MediaTransferResult, execute_media_transfer
from cassn.ndp.transfer import (
    DeploymentTransfer,
    MediaTransferPlan,
    NdpTransferError,
    TransferFile,
    plan_media_transfer,
)

__all__ = [
    "ApplyResult",
    "MANIFEST_TYPE",
    "MANIFEST_VERSION",
    "ManifestBuild",
    "MediaTransferPlan",
    "MediaTransferResult",
    "MetadataDocument",
    "NdpStagingError",
    "NdpTransferError",
    "PlannedDeployment",
    "SNAPSHOT_VERSION",
    "StagingPlan",
    "TransferFile",
    "DeploymentTransfer",
    "apply_plan",
    "build_manifest",
    "content_digest",
    "execute_media_transfer",
    "plan_media_transfer",
    "plan_event",
    "read_event_documents",
    "read_event_recorded_by",
]
