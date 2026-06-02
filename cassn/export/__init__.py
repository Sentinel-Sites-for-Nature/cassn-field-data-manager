"""
Output generation: the metadata CSVs and the Wildlife Insights deployment CSVs.

These modules turn a finished ``file_inventory`` (plus the lookup tables) into
the on-disk artifacts a deployment ships with. They are stdlib-only and take
their inputs explicitly, so the CLI tools in ``utils/`` reuse the same builders
the GUI does — there is exactly one ``WI_COLUMNS`` schema and one event-name
convention in the codebase.
"""
