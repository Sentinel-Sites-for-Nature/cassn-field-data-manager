# Deployment lookup data contract

This document defines the authoritative runtime relationship between deployment
events and individual device deployments. It applies prospectively to ingest
performed after this contract is adopted. Historical deployment folders,
filenames, metadata, and identifiers that have already been filed remain valid
under the application version that created them and are not regenerated.

## Authorities

`deployment_events.csv` is the authority for completed protocol intervals that
can be selected in the application and for their event-level folder identity.
It is a lightweight grouping table: an individual deployment may exist before
the containing event is complete and assigned.

Required columns:

```text
deployment_event_id,site_short_name,site_name,
deployment_event_start_date,deployment_event_end_date
```

`deployments.csv` is the authority for every individual monitoring interval
available for ingest.

Required columns:

```text
deployment_id,deployment_event_id,deployment_sequence,site_short_name,
plot_number,device_type,deployment_start_date,deployment_end_date,
identifier_policy,device_id,asset_tag,feature_type,mounted_on,
sensor_height_meters
```

The schema intentionally contains only fields required by the application or
its current Wildlife Insights and SoundHub outputs. `device_id` is the physical
device identifier for every device type and is also the downstream `camera_id`
for ML and SA rows. For BD and BT rows it is the 16-character uppercase
hexadecimal AudioMoth serial reported by the recorder. `asset_tag` preserves the
four-digit field inventory label for ARUs and is blank for cameras. There is no
duplicate `camera_id` lookup column. A physical identifier may remain blank
when no recorder-derived or filed source supports it; `asset_tag` must never be
substituted into `device_id`.

Each field has one authority. Camera `feature_type`, ARU `mounted_on`, and ARU
`sensor_height_meters` come from the deployment Survey123 record. Camera
height/orientation and other platform/protocol constants remain in the relevant
JSON configuration. Recorder gain, sample rate, filters, and recording schedule
come from ingest metadata (CONFIG.TXT/WAV), not this lookup. The runtime does not
silently substitute one source for another: a missing required authoritative
value is a validation/review issue, and an optional missing value remains blank.

An already-filed historical deployment whose immutable identifier does not
follow the prospective formula may carry the optional value
`identifier_policy=filed_legacy`. This is a narrow compatibility exception,
not a general validation bypass: it is used only when the exact identifier is
already present in deployment metadata, Box, Wildlife Insights, or SoundHub.
Rows without that evidence leave the field blank and must follow the current
formula.

There is no required `devices.csv`. Device-history fields such as first/last
seen dates, deployment counts, Survey123 roles, and source GlobalIDs are not
runtime lookup data. A hardware-history report can be derived from deployments
and media metadata later if stable hardware identities become useful.

## Definitions

A **deployment event** is a protocol-defined monitoring interval at one site.
It may contain the normal full complement of plots and device types or a
purpose-specific subset. The event dates provide grouping context; they do not
overwrite an individual deployment's dates. A completed deployment can retain
its ID and end date while `deployment_event_id` remains blank until the broader
event is complete.

A **deployment** is one monitoring interval for one plot and device type. A
deployment ends at a protocol boundary: card/service retrieval, physical
removal, failure/replacement, or another event boundary. A device that remains
mounted across a card and battery service therefore ends one deployment and
starts another.

The physical card or source directory is an ingest unit bound to one selected
deployment. It is not a canonical lookup entity.

## Identifier rules for new deployments

The base deployment identifier is:

```text
ORG_<site_short_name>_plot<plot_number>_<device_type>_<deployment_end_YYYYMMDD>
```

`deployment_sequence` is a non-negative integer that orders deployments of the
same device type at the same plot within one eventual deployment event.
Sequence zero uses the base identifier. A successor created by a mid-event
redeployment is assigned sequence 1 immediately, even while open; later
successors use 2, 3, and so on. Because the deployment's own end date normally
makes its ID unique, sequence is not ordinarily encoded in the ID. Only when
two deployments for the same site, plot, device type, and end date would
otherwise collide does a nonzero sequence add `-seq01`, `-seq02`, and so on.
The explicit `seq` marker avoids collision with NDP file/record version labels.
Once an identifier has been published, its sequence and identifier are
immutable.

All renamed media filenames use the exact deployment identifier as their
prefix. `deployment_event_id` remains a separate metadata field and the event
folder name.

## Validation invariants

For new runtime lookup snapshots:

- every closed deployment has a globally unique deployment ID;
- event membership is optional, but any populated event ID references a known
  event whose site agrees with the deployment;
- dates use `YYYY-MM-DD`, and end is not before start;
- event membership is explicit and is not inferred from date containment;
- `(deployment_event_id, site_short_name, plot_number, device_type,
  deployment_sequence)` is unique;
- `deployment_id` is globally unique and follows the date/sequence rule;
- a nonconforming historical ID is accepted only when explicitly marked
  `identifier_policy=filed_legacy` and verified against an existing filed
  artifact;
- open deployments have neither an event ID nor a deployment ID, but may have
  a nonzero sequence assigned by a known mid-event redeployment;
- no missing retrieval date may be replaced with a plausible inferred date;
- every populated BD/BT `device_id` is a 16-character uppercase hexadecimal
  AudioMoth serial, and every populated ARU `asset_tag` is four digits;
- camera rows have a blank `asset_tag`;
- one AudioMoth serial cannot occupy overlapping deployment slots in the same
  event or pending field round;
- retrieval matching must use source timestamps and may not close a successor
  that started at or after the retrieval timestamp.

Same-day boundaries are valid because a field crew may retrieve one card and
start the successor deployment on the same visit. The automated workflow must
use the source datetimes to distinguish that legitimate boundary from a
retrieval incorrectly applied to both deployments.

An individual deployment may begin before the event-level start date or end on
an adjacent date. Event dates are grouping and naming context, not bounds that
overwrite or invalidate the deployment interval.

## Deferred fields

Compass bearings are deliberately deferred because current downstream exports
do not require them. Future work should map `mlCameraDirection` to
`deployments.csv.camera_direction_degrees`, then carry it through
`image_file_metadata.csv` and `occurrences.csv`. If ARU microphone bearings are
later needed, normalize them separately per device row: BD from
`bird_recorder_orientation`, BT from `bat_recorder_orientation`, and a single
ARU from `aru_recorder_orientation`. Do not confuse these numeric bearings with
the Wildlife Insights categorical camera-orientation constants.

## Historical compatibility

The application does not scan or revalidate historical deployment folders
against the prospective identifier formula. Existing Box folder names,
filenames, deployment IDs, Wildlife Insights records, SoundHub keys, metadata
CSVs, and event records remain unchanged.

When historical records are represented in a curated lookup or migration
report, existing filed identifiers take precedence over regenerated Survey123
identifiers. Corrections to known bad ingests are deliberate, deployment-level
maintenance operations rather than a bulk reprocessing requirement.
