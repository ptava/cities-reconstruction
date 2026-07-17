# External Data Sources

This file records external datasets and services used or referenced by the
project. Statuses describe the current Florence example as verified on
2026-07-17.

## Status

- **Local**: a copy is currently present under `docs/assets`.
- **Runtime**: requested from the provider when the relevant stage runs; not
  stored in the source tree.

## Source inventory

| Source | Provider | Status | Project use | License |
|---|---|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) via [Overpass API](https://overpass-api.de/) | OpenStreetMap contributors | Runtime | Buildings, surfaces, roads, water and trees retrieved by `shapefiles` | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/) |
| [DTM 2023](https://opendata.comune.fi.it/page_dataset_show?id=dtm-lidar-2023) | Comune di Firenze | Local | Ground elevations for point-cloud generation | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [DSM 2023](https://opendata.comune.fi.it/page_dataset_show?id=dsm-lidar-2023) | Comune di Firenze | Local | Surface elevations for point-cloud generation | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [Aree stradali](https://opendata.comune.fi.it/page_dataset_show?id=a44ea551-7b09-4d0d-af64-7c08159e9316) | Comune di Firenze | Local | Supplemental road polygons | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [Aree verdi](https://opendata.comune.fi.it/page_dataset_show?id=d0ffa579-e002-4dc3-94b4-d69207cae114) | Comune di Firenze | Local | Supplemental green-area polygons | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [Alberi](https://opendata.comune.fi.it/page_dataset_show?id=42cd1073-521f-4040-9491-e993d03663a4) | Comune di Firenze | Local | Supplemental municipal tree inventory | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [OFC_RT orthophotos](https://www502.regione.toscana.it/geoscopio/servizi/wms/OFC_RT.htm), layer `rt_ofc.5k24.32bit` | Regione Toscana | Runtime | Optional graphical QA imagery | Creative Commons; verify the layer-specific terms before redistribution |
| [FOTOTECA aerial images](https://www502.regione.toscana.it/geoscopio/servizi/wms/FOTOTECA.htm), layer `rt_fotogrammi.2024.rt` | Regione Toscana | Runtime | Optional graphical QA imagery | Creative Commons; verify the layer-specific terms before redistribution |

## Local paths and provenance

### Comune di Firenze elevation data

- DTM: `docs/assets/dtm/DTM_2023`
- DSM: `docs/assets/dsm/DSM_2023`
- CRS used by the Florence configuration: `EPSG:25832`
- Original LiDAR survey date: 2023-06-27
- Bundled metadata and license files:
  - `docs/assets/dtm/DTM_2023/METADATO_E_LICENZA/`
  - `docs/assets/dsm/DSM_2023/METADATO_E_LICENZA/`

### Comune di Firenze vector data

- Aree stradali: `docs/assets/data/florence_opendata/streets/`
- Aree verdi: `docs/assets/data/florence_opendata/green_areas/`
- Alberi source data: `docs/assets/data/florence_opendata/trees/`
- CRS: `EPSG:3003`

The Florence configuration loads the diameter-enriched tree copy under
`docs/assets/data/florence_opendata/trees_diameter/`. That copy is derived from
the external **Alberi** dataset and is not a separate external source.

### Runtime services

- Overpass endpoint: `https://overpass-api.de/api/interpreter`
- OFC_RT WMS endpoint:
  `https://www502.regione.toscana.it/ows_ofc/com.rt.wms.RTmap/wms?map=owsofc_rt&`
- FOTOTECA WMS endpoint:
  `https://www502.regione.toscana.it/wmsraster/com.rt.wms.RTmap/wms?map=wmsfotogrammi&`

Runtime responses and generated imagery are written beneath `outputs/`, which
is excluded from version control.

## Attribution

When publishing outputs derived from OpenStreetMap, include:

> © OpenStreetMap contributors; data available under the Open Database License.

When redistributing or publishing outputs derived from Comune di Firenze data,
credit **Comune di Firenze**, name the dataset, link its catalog page, and state
that it is licensed under CC BY 4.0.

For Regione Toscana WMS imagery, credit **Regione Toscana**, identify the WMS
service and layer, and confirm the applicable Creative Commons terms before
redistributing downloaded imagery.

Project-authored planning data, generated geometry, derived datasets and model
libraries are outside the scope of this external-source inventory.
