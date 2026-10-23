"""
01_arcgis_metadata_retrieve_prep.py

Downloads CSU SSN deployment and plot tables from ArcGIS Online (public
Feature Services) and preps them for later use by splitting BDBT deployment
rows into separate BD and BT rows via bd_bt_deployment_separator().

Dependencies:
    pip install arcgis pandas
"""

import pandas as pd
from arcgis.features import FeatureLayer
from arcgis.gis import GIS

# Local helper — equivalent to sourcing BDBT_Deployment_Separator.R
from bdbt_deployment_separator import bd_bt_deployment_separator

# ---------------------------------------------------------------------------
# ArcGIS REST URLs for CSU SSN public layers
# ---------------------------------------------------------------------------
PDD_URL = (
    "https://services1.arcgis.com/0j6vZbECadDEXdAS/ArcGIS/rest/services"
    "/CSU_SSN_Public_Deployment_Data/FeatureServer/0"
)
PPD_URL = (
    "https://services1.arcgis.com/0j6vZbECadDEXdAS/ArcGIS/rest/services"
    "/CSU_SSN_Public_Plot_Data/FeatureServer/0"
)

# ---------------------------------------------------------------------------
# Connect anonymously (public layers — no login required)
# ---------------------------------------------------------------------------
gis = GIS()  # anonymous connection; swap for GIS("home") if credentials needed

# ---------------------------------------------------------------------------
# Open layers and inspect available fields
# ---------------------------------------------------------------------------
pdd_layer = FeatureLayer(PDD_URL, gis=gis)
ppd_layer = FeatureLayer(PPD_URL, gis=gis)

# List fields — equivalent to list_fields() in {arcgis}
pdd_fields = pd.DataFrame(
    [{"name": f["name"], "type": f["type"], "alias": f["alias"]}
     for f in pdd_layer.properties.fields]
)
ppd_fields = pd.DataFrame(
    [{"name": f["name"], "type": f["type"], "alias": f["alias"]}
     for f in ppd_layer.properties.fields]
)

print("=== Deployment layer fields ===")
print(pdd_fields.to_string(index=False))
print("\n=== Plot layer fields ===")
print(ppd_fields.to_string(index=False))

# ---------------------------------------------------------------------------
# Download full tables — equivalent to arc_select()
# query() returns a FeatureSet; converting via .features avoids the
# spatial extension dependency that as_df=True requires.
# ---------------------------------------------------------------------------
def featureset_to_df(feature_layer: FeatureLayer) -> pd.DataFrame:
    """Query all features from a layer and return a plain pandas DataFrame."""
    feature_set = feature_layer.query()
    records = [f.attributes for f in feature_set.features]
    return pd.DataFrame(records)

csu_ssn_deployments: pd.DataFrame = featureset_to_df(pdd_layer)
csu_ssn_plots: pd.DataFrame = featureset_to_df(ppd_layer)

# ---------------------------------------------------------------------------
# Duplicate-check: deployments (keyed on deployment_id + visit_type)
# ---------------------------------------------------------------------------
dup_deployments = (
    csu_ssn_deployments
    .groupby(["deployment_id", "visit_type"])
    .size()
    .reset_index(name="n")
    .query("n > 1")
)
if dup_deployments.empty:
    print("\nNo duplicate deployments found.")
else:
    print("\nDuplicate deployments:")
    print(dup_deployments)

# ---------------------------------------------------------------------------
# Duplicate-check: plots (keyed on plot_id)
# ---------------------------------------------------------------------------
dup_plots = (
    csu_ssn_plots
    .groupby("plot_id")
    .size()
    .reset_index(name="n")
    .query("n > 1")
)
if dup_plots.empty:
    print("\nNo duplicate plots found.")
else:
    print("\nDuplicate plots:")
    print(dup_plots)

# ---------------------------------------------------------------------------
# Inspect table structure — equivalent to str()
# ---------------------------------------------------------------------------
print("\n=== Deployment table structure ===")
print(csu_ssn_deployments.info())
print(csu_ssn_deployments.dtypes)

# ---------------------------------------------------------------------------
# Run the BDBT deployment separator
# ---------------------------------------------------------------------------
csu_ssn_deployments_bdbt_prepped = bd_bt_deployment_separator(csu_ssn_deployments)
