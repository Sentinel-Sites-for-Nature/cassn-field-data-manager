# ArcGIS Hosted Metadata Lookup Migration Notes

## Purpose

This document captures planning notes for replacing the current Box-hosted lookup
CSV/config system with an ArcGIS-hosted metadata lookup layer or set of layers.
It is intended as durable project memory so the migration can be resumed later
without relying on chat history.

## Current Production System

The app currently populates metadata from three broad sources:

- File-derived metadata: EXIF, Reconyx MakerNote/ExifTool fields, AudioMoth WAV
  comments, GUANO chunks, CONFIG.TXT files, hashes, file sizes, and timestamps.
- User/session metadata: organization, site, deployment dates, observer, selected
  devices, staging folder, and upload provenance.
- Lookup/config metadata: Box-synced CSV and JSON files cached under
  `~/.cassn_config/lookup_tables/`.

The Box-synced stable lookup/config files are:

- `sites.csv`
- `plots.csv`
- `wi_config.json`
- `soundhub_config.json`
- `program_config.json`

Survey123 separately supplies `devices.csv` and device-level `deployments.csv`.
The app does not use `cameras.csv`, `ARUs.csv`, or event-only `deployments.csv`
as active inputs. Its camera and ARU compatibility views are derived from the
selected rows in device-level `deployments.csv`.

## Target Architecture

The long-term goal is to replace the lookup CSV/config files with an ArcGIS
hosted metadata service derived from Survey123 submissions.

Conceptually:

```text
Survey123 deployment/check/retrieval records
  -> Python ETL in this repo
  -> standardized ArcGIS hosted metadata table/layer(s)
  -> Field Data Manager metadata lookup
  -> Wildlife Insights / Wildlife SoundHub-ready metadata outputs
```

The key design principle is to keep the app-facing metadata schema stable. The
app should not need to know whether metadata came from old Survey123 forms, new
CDFW Survey123 forms, Box CSVs, or hand-curated defaults. The ETL should adapt
source data into a stable lookup contract.

## Recommended Migration Strategy

Do not replace the production Box lookup system before the new CDFW Survey123
forms are adopted and stable.

Recommended path:

1. Keep Box CSV/config lookups in production for current data downloads.
2. Build the ArcGIS ETL as a parallel prototype.
3. Use current Survey123 exports to validate the target schema and field logic.
4. Adopt the new CDFW forms as the future source contract.
5. Update only the extraction/mapping adapter for the new forms.
6. Switch the app from Box lookups to ArcGIS lookups only after the ArcGIS output
   matches expected metadata and edge cases are handled.

This avoids locking the app into the messy current Survey123 export aliases while
still allowing the hosted lookup design to move forward.

## Preferred Data Model

The replacement should not be only a hosted clone of `plots.csv`. The Survey123
data has deployment-specific device coordinates and setup details, so the
central app-facing object should be a device deployment metadata record.

Likely hosted tables/layers:

- `device_deployments`: one row per deployed device event.
- `retrievals`: latest retrieval/check status by site, plot, device type, and
  deployment period.
- `metadata_defaults`: replacement for static values currently held in
  `wi_config.json` and `soundhub_config.json`.
- `sites`: canonical `site_name`, `site_short_name`, and `site_code` identity.

Likely key fields for the app-facing lookup:

- `site_name`
- `site_short_name`
- `site_code`
- `plot_number`
- `device_type`
- `deployment_start`
- `deployment_end`
- `camera_id` or `device_id`
- `latitude`
- `longitude`
- Wildlife Insights fields
- Wildlife SoundHub fields
- status fields
- notes/provenance fields

The practical app query should be similar to:

```text
For site X, plot N, device type D, and deployment period P,
return the metadata needed to write image/audio metadata rows.
```

## Current Lookup-Derived Metadata Fields

### From `sites.csv`

Used in both image and audio metadata:

- `site_name`: full formal name
- `site_short_name`: stable relational key and deployment-ID token
- `site_code`: acronym

### From `plots.csv`

Used in both image and audio metadata:

- `latitude`
- `longitude`

The current app gets these from `plot_latitude` and `plot_longitude`.

### From device-level `deployments.csv` camera rows

Used in `image_file_metadata.csv` and later reshaped into Wildlife Insights
deployment CSVs:

- `camera_id`
- `feature_type`
- `sensor_height`
- `sensor_orientation`
- `plot_treatment`
- `plot_treatment_description`
- `detection_distance`

### From device-level `deployments.csv` ARU rows

Used in `audio_file_metadata.csv`:

- `sensor_height_meters`
- `ARU_status`

The app creates event-scoped camera and ARU compatibility views from the
selected Survey123 deployment round; the retired camera and ARU CSVs are not
read at runtime.

## Current JSON-Derived Metadata Fields

### From `wi_config.json`

Used in `image_file_metadata.csv` and Wildlife Insights exports:

- `project_id`
- `bait_type`
- `bait_description`
- `event_type`
- `quiet_period`
- `camera_functioning`

These are currently keyed by device type where needed, for example
`project_id_ML`, `project_id_SA`, `bait_type_ML`, and `bait_type_SA`.

### From `soundhub_config.json`

Used in `audio_file_metadata.csv`, usually as defaults/fallbacks after
file-derived metadata:

- `ARU_make`
- `ARU_model`
- `sample_rate_hz`
- `deployment_start_time`
- `deployment_end_time`
- `frequency`
- `duration`
- `feature_type`
- `ARU_container`
- `ARU_microphone`
- `mounted_on`

Some audio fields should continue to prefer file-derived values first:

- `ARU_make`
- `ARU_model`
- `sample_rate_hz`
- `gain`
- `filter_type_khz`
- `deployment_start_time`
- `deployment_end_time`
- `frequency`
- `duration`
- `filter_type_duration`
- `filter_type_amplitude`

The hosted defaults should be fallbacks, not overrides, for those fields.

## Survey123 Sample Exports Reviewed

Four current Survey123 exports were reviewed:

- `ML_Camera_Deployment___UCNRS_SSN_0 3.csv`
- `Small_Animal_Camera_Deployment_0 2.csv`
- `ARU_Deployment___CA_SSN_0 2.csv`
- `Sentinel_Site_Data_Retrieval___UCNRS_0.csv`

Record counts observed:

- ML camera deployment: 169 data rows
- Small animal camera deployment: 167 data rows
- ARU deployment: 198 data rows
- Retrieval: 66 data rows

The exports use user-facing field aliases, including repeated names such as
`Specify other.` and repeated labels for some retrieval fields. The future ETL
should use actual ArcGIS field names from hosted layers, not CSV export aliases,
to avoid ambiguity.

## Survey123 Field Mapping Takeaways

### Common deployment identity

Likely sources:

- `Site ID` (Survey123 acronym) -> canonical `site_short_name` via `sites.csv`
- `Plot` or `Plot number` -> `plot_number`
- `Date` -> deployment date/start context
- deployment end fields such as `deployment_end_time`, `Deployment end datetime`,
  or `calcEndDateTime` -> deployment completion context
- `Crew` -> possible observer/recorded_by source, though the app currently uses
  the selected downloader/observer

### ML camera deployment

Likely sources:

- `camera_ID` -> `camera_id`
- `latitude`, `longitude` or `x`, `y` -> camera deployment coordinates
- `Camera Location` -> Wildlife Insights `feature_type`
- compass-bearing field -> possible `sensor_orientation` or separate bearing
  field, depending on future schema
- `Gusto deployed?` -> bait-related logic
- `notes` -> setup notes
- `Python lock used?`, `Security enclosure used?`, and mounting fields -> useful
  operational metadata, not currently in metadata CSVs

Current values suggest `Camera Location` choices such as `Trail_game`,
`Water_source`, `Road_dirt`, `Trail_hiking`, `Road_paved`, `Burrow`, and `other`.
These will likely need normalization to Wildlife Insights accepted labels.

### Small animal camera deployment

Likely sources:

- `camera_ID` -> `camera_id`
- `latitude`, `longitude` or `x`, `y` -> camera deployment coordinates
- `camera_habitat` -> likely Wildlife Insights `feature_type` or a derived
  feature/habitat classification
- `Was a half-turnaround used?` -> small-animal setup metadata
- `pine_sol` and bait/protection fields -> bait/setup metadata
- `notes` -> setup notes

`camera_habitat` can contain multiple comma-separated values, so the ETL needs a
rule for selecting or normalizing a single `feature_type` when a downstream
metadata standard expects one value.

### ARU deployment

Likely sources:

- `audiomoth_bird_ID` -> bird ARU `device_id`, where filled
- `audiomoth_bat_ID` -> bat ARU `device_id`, where filled
- `recorders_geo_x`, `recorders_geo_y` -> shared ARU coordinates when bird and
  bat are colocated
- `bird_geo_x`, `bird_geo_y` and `bat_geo_x`, `bat_geo_y` -> device-specific
  coordinates if separated
- `BIRD Audiomoth Height (m)` -> BD `sensor_height_meters`
- `BAT Audiomoth Height (m)` -> BT `sensor_height_meters`
- `Recorders are mounted on:` or device-specific mounted-on fields -> `mounted_on`
- bearing fields -> possible microphone orientation/bearing metadata
- `Notes:`, `Bird Installation Notes`, `Bat Installation Notes`,
  `ARU Installation Notes` -> setup notes

The current exports include both paired-ARU and single-ARU structures. The ETL
should expand each ARU survey record into one or two device deployment rows,
depending on whether bird, bat, or both were deployed.

### Retrieval form

Likely sources:

- `Site ID`, `Plot` -> retrieval key
- `User-reported Retrieval End Date and Time` -> deployment end/retrieval time
- `Equipment Tampered With`, `Tampering Culprit` -> operational status
- `Functionality check` and `Disfunctional Recorders` -> ARU status
- `Recorders with precipitation inside the case` -> ARU condition/status
- `AM Bird Battery`, `AM Bat Battery` -> retrieval battery condition
- `Recorder Retrieval Notes`, `ML Retrieval Notes`, `SA Retrieval Notes` -> notes
- `small animal SD card ID`, `ML SD card ID` -> retrieval media IDs
- ML/SA date-time check fields -> useful QC, not currently central to metadata

The retrieval form should probably feed status and end-date fields into the
standard metadata layer, but it should not overwrite raw deployment setup fields
without explicit conflict rules.

## Static Defaults Still Needed

Even with Survey123 as the source of truth, some metadata remains program-level
or device-type-level configuration rather than survey-collected data.

Candidate `metadata_defaults` fields:

- `device_type`
- `project_id`
- `event_type`
- `quiet_period`
- `camera_functioning_default`
- `bait_type_default`
- `bait_description_default`
- `ARU_make_default`
- `ARU_model_default`
- `ARU_microphone`
- `ARU_container`
- `sample_rate_hz_default`
- `deployment_start_time_default`
- `deployment_end_time_default`
- `frequency_default`
- `duration_default`
- `feature_type_default`
- `mounted_on_default`

The ETL should record whether a final value came from Survey123, file metadata,
or defaults whenever practical.

## Open Design Questions

- Should the app query ArcGIS directly at runtime, or should it sync ArcGIS
  lookup tables into a local cache first?
- Should hosted lookup outputs be one consolidated app-facing table or several
  normalized tables?
- How should multiple current Survey123 records for the same site/plot/device be
  resolved: latest edit date, deployment period, explicit status, or manual
  review?
- How should old-form and new CDFW-form records coexist during the transition?
- Which Survey123 fields should be considered authoritative for deployment start
  and end dates?
- Should all three canonical site fields remain sourced from a hosted `sites`
  table, or should Survey123-derived output carry a redundant snapshot?
- Should `mounted_on` move from static `soundhub_config.json` behavior to
  ARU-deployment survey values?
- How should multi-select habitat/location values map to single-value downstream
  standards such as Wildlife Insights `feature_type`?
- Should operational fields like locks, enclosures, fence status, SD card IDs,
  and notes be preserved in the app-facing layer even if they are not currently
  written to metadata CSVs?

## Recommended Next Steps

When ready to resume:

1. Get the current ArcGIS hosted layer field names for the Survey123 forms,
   preferably from the ArcGIS REST schema rather than CSV aliases.
2. Define the stable app-facing metadata schema before writing production code.
3. Create a read-only Python ETL prototype in this repo.
4. Have the prototype read old/current Survey123 exports or hosted layers.
5. Write a candidate standardized table locally as CSV/GeoJSON for inspection.
6. Compare candidate outputs against the current Box lookup-derived metadata.
7. Repeat after adopting the new CDFW Survey123 forms.
8. Only then update the app lookup provider from Box CSV/config files to ArcGIS.

## Implementation Notes For Future Development

Likely Python dependencies:

- `arcgis` for authenticated ArcGIS Online access
- `pandas` for tabular transforms
- possibly `geopandas` only if spatial operations become necessary

Likely code shape:

- Keep the current `LookupTables` interface stable at first.
- Add an alternate lookup provider that can load from ArcGIS-derived tables.
- Avoid changing metadata CSV writers until the ArcGIS-derived lookup output can
  mimic the current lookup contract.
- Add tests around mapping functions before switching the GUI to the new source.

The safest first implementation is not "replace Box lookups." It is:

```text
ArcGIS/Survey123 source -> ETL -> local standardized lookup snapshot
```

Once that snapshot matches expectations, the same schema can be published as a
hosted ArcGIS table and consumed by the app.
