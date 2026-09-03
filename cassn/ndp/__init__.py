"""
National Data Platform staging for the CA-SSN source namespace.

Box holds the archival originals. NDP gets a per-deployment copy beneath a
user-specified OSDF source collection root::

    <source-root>/<deployment_event_id>/<deployment_id>/

Each deployment is described by a ``manifest.json`` and a
``metadata/file_metadata.csv`` partitioned out of the deployment event's filed
metadata. This package builds the description and transfers media with hash
verification; nothing here writes to Box.

Named for the destination platform rather than the transport, matching
:mod:`cassn.soundhub`. OSDF is the storage federation and Pelican the transfer
tool; both are mechanisms and belong in modules inside this package.

Layers, bottom up:

* :mod:`cassn.ndp.manifest` — the ``cassn.source.deployment`` document, built
  from metadata rows and a curated lookup row. Pure: no filesystem, no clock,
  no network.
* :mod:`cassn.ndp.staging` — read one event's metadata, split it per
  deployment, render the output tree, and apply it idempotently.
* :mod:`cassn.ndp.transfer` — resolve the described inventory to exact Box
  objects and a user-specified OSDF destination.
* :mod:`cassn.ndp.submission` — download, verify, sync, and record resumable
  media-transfer state without yet publishing control files.

Neither imports Qt.
"""

from cassn.ndp.manifest import (
    INVENTORY_REVISION,
    MANIFEST_TYPE,
    SCHEMA_VERSION,
    ManifestBuild,
    build_manifest,
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
    "INVENTORY_REVISION",
    "MANIFEST_TYPE",
    "SCHEMA_VERSION",
    "ManifestBuild",
    "MediaTransferPlan",
    "MediaTransferResult",
    "MetadataDocument",
    "NdpStagingError",
    "NdpTransferError",
    "PlannedDeployment",
    "StagingPlan",
    "TransferFile",
    "DeploymentTransfer",
    "apply_plan",
    "build_manifest",
    "execute_media_transfer",
    "plan_media_transfer",
    "plan_event",
    "read_event_documents",
]
