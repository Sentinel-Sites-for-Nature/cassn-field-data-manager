"""
Function to separate BD/BT records from deployments where they were deployed
on the same post, resulting in a table where each BDBT deployment is separated
into BD and BT deployments.
"""

import pandas as pd


def bd_bt_deployment_separator(deployment_table: pd.DataFrame) -> pd.DataFrame:
    """
    Separate BDBT deployment rows into individual BD and BT rows.

    For deployments where both a Bird and Bat AudioMoth were placed on the same
    post (aru_taxon == "BDBT"), this function splits each such row into two rows
    — one for BD and one for BT — reassigning the "_2" column values to the BD
    row and dropping the "_2" columns from the output.

    Parameters
    ----------
    deployment_table : pd.DataFrame
        The raw deployment table containing BDBT rows and paired "_2" columns.

    Returns
    -------
    pd.DataFrame
        Deployment table with BDBT rows replaced by separate BD and BT rows.
    """

    # Get names of columns that hold data for the second piece of equipment
    # (i.e. columns ending in "_2")
    equip_columns_2 = [col for col in deployment_table.columns if col.endswith("_2")]

    # Derive the "base" column names by stripping the "_2" suffix
    equip_columns = [col[:-2] for col in equip_columns_2]

    # --- Separate BDBT rows into BD and BT ---
    bdbt_rows = deployment_table[deployment_table["aru_taxon"] == "BDBT"].copy()

    # Duplicate each BDBT row: one copy becomes BD, one becomes BT
    bd_rows = bdbt_rows.copy()
    bt_rows = bdbt_rows.copy()
    bd_rows["aru_taxon"] = "BD"
    bt_rows["aru_taxon"] = "BT"

    # For BD rows, overwrite base columns with the corresponding "_2" values
    # (the second AudioMoth — the Bird device — uses the "_2" fields)
    for base_col, col_2 in zip(equip_columns, equip_columns_2):
        if base_col in bd_rows.columns and col_2 in bd_rows.columns:
            bd_rows[base_col] = bd_rows[col_2]

    # Combine the BD and BT halves, then drop the "_2" columns
    bd_bt_separated = pd.concat([bd_rows, bt_rows], ignore_index=True)
    bd_bt_separated = bd_bt_separated.drop(
        columns=[c for c in equip_columns_2 if c in bd_bt_separated.columns]
    )

    # --- Build non-BDBT portion of the table ---
    deployment_table_no_bdbt = deployment_table[
        deployment_table["aru_taxon"] != "BDBT"
    ].copy()
    deployment_table_no_bdbt = deployment_table_no_bdbt.drop(
        columns=[c for c in equip_columns_2 if c in deployment_table_no_bdbt.columns]
    )

    # --- Rejoin separated rows with the rest of the table ---
    bd_bt_separated_deployment_table = pd.concat(
        [deployment_table_no_bdbt, bd_bt_separated], ignore_index=True
    )

    # --- Summary messages ---
    n_bdbt   = (deployment_table["aru_taxon"] == "BDBT").sum()
    n_row_pre  = len(deployment_table)
    n_bd_pre   = (deployment_table["aru_taxon"] == "BD").sum()
    n_bt_pre   = (deployment_table["aru_taxon"] == "BT").sum()
    n_row_post = len(bd_bt_separated_deployment_table)
    n_bd_post  = (bd_bt_separated_deployment_table["aru_taxon"] == "BD").sum()
    n_bt_post  = (bd_bt_separated_deployment_table["aru_taxon"] == "BT").sum()

    print(f"Number of deployment records in source data: {n_row_pre}.")
    print(
        f"Number of deployment records with both Bird and Bat AudioMoths "
        f"at the same location: {n_bdbt}."
    )
    print(
        f"Number of Bird records added: {n_bd_post - n_bd_pre}. "
        f"Number of Bat records added: {n_bt_post - n_bt_pre}."
    )
    print(f"Number of deployment records in output data: {n_row_post}.")

    return bd_bt_separated_deployment_table
