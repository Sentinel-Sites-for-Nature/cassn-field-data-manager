# Sentinel Sites for Nature – California branding

`ssn-california-map.png` is the unchanged image John supplied as
`Sentinel-Site-Network-Dashboard.030526.png` on 2026-10-23. The organization
selector places it beside the Sentinel Sites for Nature – California heading
and Data Manager Application subtitle.

The header's dark green surface and bold, spaced, white capital letters follow
John's screenshot of the [CA-SSN website](https://cassn.org/).

The current header uses `ssn-california-map-transparent.png`, a transparent
rendition prepared with ImageGen to remove the exterior white background. The
unchanged supplied map is retained as the source. The state displays larger,
and **Data Manager Application** is the dominant text below the network name.

The current app icon is `ssn_data_manager_icon.svg`: centered **SSN**, **Data**,
and **Manager** on three rows, in white on a dark green rounded square. Qt renders
the SVG into the accompanying PNG; PyInstaller's `normalize_icon_type` converts
that to ICNS. The bundle uses `ssn_data_manager_icon.icns`, and the application
and workspace windows use `ssn_data_manager_icon.png`.

## Previous map-based icon (retained source assets)

`cassn_map_icon.png` is a square icon rendition of the supplied map: proportional
scaling and white padding preserve the entire state, without stretching or
cropping. Native macOS `sips` prepared the square 1024-pixel PNG;
PyInstaller's `normalize_icon_type` converted it into `cassn_map_icon.icns`,
including the standard macOS icon sizes. The bundle uses the ICNS, and Qt uses
the PNG for the application/window icon. Both are stored in the repository
alongside the original map, so builds do not depend on John's Downloads folder.
