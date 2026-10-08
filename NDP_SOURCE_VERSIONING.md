# CASSN NDP source versioning

The NDP `source/` collection is append-only by object path. Pelican and OSDF
caches may retain an object after first use, so a path that has ever held an
object is never reused for different bytes.

## Global suffix rule

An untagged object is version 00. A corrected logical object takes the next
monotonically increasing suffix immediately before its extension: `-v01`,
`-v02`, and so on. A deleted suffix is never reused or filled as a gap.

The `-vNN` ending is reserved for CASSN versioning. New version-00 logical
filenames must not naturally end with that pattern.

Examples:

- `IMG_0042.jpg` is file version 00.
- `IMG_0042-v01.jpg` is the first corrected version of that image.
- `metadata/file_metadata.csv` and `manifest.json` are inventory revision 00.
- `metadata/file_metadata-v01.csv` and `manifest-v01.json` are inventory
  revision 01.

Deletion is permitted when required, but a deleted path is still retired and
must not be reused. Superseded objects should normally be retained so an older
inventory remains reproducible.

## Two independent revision counters

Media are versioned per logical file. Correcting one image or recording never
requires re-uploading the deployment's unchanged media.

The inventory and manifest use a separate deployment-level control revision.
They advance together whenever the authoritative selection of files changes.
Their revision does not equal the revision of every media object they list. For
example, inventory revision 02 may select `IMG_0042-v01.jpg`,
`IMG_0100-v01.jpg`, and thousands of unchanged version-00 files.

The deployment directory itself is stable and is not a snapshot. It may contain
a mixture of current and superseded object versions.

## Determining the current deployment

The highest numbered **complete manifest revision** is authoritative. Revision
00 uses the untagged `manifest.json`; later revisions use `manifest-vNN.json`.
A manifest revision is complete only when:

1. its referenced `metadata/file_metadata[-vNN].csv` exists;
2. that inventory's SHA-256 matches `content.inventory.sha256`; and
3. every filename selected by the inventory exists and matches its recorded
   SHA-256.

Directory contents alone are not the current dataset. Objects absent from the
authoritative inventory are superseded or unrelated. This manifest-first rule
also prevents an interrupted upload of a new inventory from becoming current
before its matching manifest is committed.

## Source publication order

For each initial publication or correction:

1. Transfer new media objects to never-before-used paths.
2. Verify every new object against its recorded SHA-256.
3. Stamp internal provenance and build the next inventory and manifest in local
   scratch space.
4. Upload the versioned `file_metadata` inventory.
5. Upload the matching versioned manifest last; its successful appearance is
   the commit point.
6. Update any collection-level deployment index only after the manifest is
   readable and validates the inventory.

Control files are never uploaded as pre-transfer drafts and are never modified
in place. Version 00 therefore means the state after a verified initial
transfer.

## Current implementation boundary

Initial version-00 media transfer and verification exist. The correction
workflow does not yet allocate `-vNN` paths, advance `inventory_revision`, or
publish regenerated control files. Those operations must not be simulated by
overwriting version-00 objects.
