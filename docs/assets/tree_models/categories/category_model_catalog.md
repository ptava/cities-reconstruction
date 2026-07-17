# Tree Category Model Catalog

This catalog is generated from `tree_categories.json`, the category OBJ files, and the Florence species/category mapping.

## DBH Scaling Rule

For the supplied Florence OpenData trees, `CIRCONF_CM` was converted to `DIAMETER_M = CIRCONF_CM / (100*pi)` in the derived DBF. Stage 1 imports `DIAMETER_M` as the tree `diameter` tag in meters.

The tree stage then applies the current implemented scaling rules:

- Species/category selection: `species_to_category[species]`, otherwise a direct alias match, otherwise the tree stage fallback model. The Florence mapping intentionally omits placeholder values such as `-` and `Da riconoscere`.
- Trunk radius from DBH: `trunk_radius_m = DBH_m / 2` when `diameter > 0.05 m`.
- Trunk top radius in generated mesh: `top_radius_m = 0.72 * trunk_radius_m`.
- Trunk height: `trunk_height_m = max(1.8, min(height_m * crown_base_fraction, height_m - 1.0))`.
- Height from explicit tag: `height_m = min(tag_height_m, 35.0)` when present and greater than `1.5 m`; otherwise the category default height is used.
- Crown radius from explicit tag: `crown_radius_m = min(crown_diameter_m / 2, height_m * 0.48)` when present and greater than `0.5 m`; otherwise `min(default_crown_radius_m, height_m * 0.42)`.
- If a circumference tag is present instead of DBH, height uses `max(4.0, min(default_height_m, 6.0 + 2.2 * sqrt(circumference_m)))`, and trunk radius uses `circumference_m / (2*pi)`.

For the current derived Florence DBF, the available per-tree DBH mainly scales the trunk radius. Height and crown radius remain category defaults unless explicit height or crown-diameter tags are present.

Source mapping: `docs/assets/data/florence_opendata/trees_diameter/species_category_mapping.json`

## Category Summary

| Category | Label | Shape | Height m | Crown radius m | Trunk radius m | Crown base fraction | Mapped species | OBJ |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| columnar_conifer | Columnar/fastigiate conifer | conical | 18 | 2.3 | 0.1449 | 0.2 | 16 | `models/columnar_conifer.obj` |
| fastigiate_broadleaf | Fastigiate/columnar broadleaf | columnar | 14 | 2.4 | 0.085 | 0.34 | 3 | `models/fastigiate_broadleaf.obj` |
| generic_round_broadleaf | Generic round broadleaf | ellipsoid | 11 | 3.5 | 0.06 | 0.35 | 49 | `models/generic_round_broadleaf.obj` |
| large_round_broadleaf | Large round/decurrent broadleaf | ellipsoid | 16 | 5.2 | 0.1399 | 0.34 | 99 | `models/large_round_broadleaf.obj` |
| palm_tuft | Palm tuft | tuft | 11 | 2 | 0.075 | 0.74 | 7 | `models/palm_tuft.obj` |
| pyramidal_conifer | Pyramidal/excurrent conifer | conical | 16 | 3.9 | 0.18 | 0.24 | 26 | `models/pyramidal_conifer.obj` |
| small_round_broadleaf | Small ornamental or fruit broadleaf | rounded | 8.5 | 2.8 | 0.0684 | 0.36 | 69 | `models/small_round_broadleaf.obj` |
| umbrella_pine | Umbrella pine | umbrella | 14 | 5.3 | 0.3024 | 0.58 | 1 | `models/umbrella_pine.obj` |
| weeping_broadleaf | Weeping broadleaf | ellipsoid | 9 | 3.8 | 0.095 | 0.42 | 5 | `models/weeping_broadleaf.obj` |

## Category Details

### columnar_conifer

- Label: Columnar/fastigiate conifer
- Basis: Narrow vertical conifer/cypress form for fastigiate urban trees.
- Crown shape: `conical`
- Default height: `18.0 m`
- Default crown radius: `2.3 m`
- Default trunk radius: `0.1449 m`
- Crown base fraction: `0.2`
- Median source diameter: `0.2899 m`
- Source record count: `9445`
- Geometry: `models/columnar_conifer.obj`
- DBH equation: `trunk_radius_m = DBH_m / 2`; mesh top radius is `0.72 * trunk_radius_m`.
- Associated species (16): `Chamaecyparis funebris`, `Chamaecyparis lawsoniana`, `Chamaecyparis nootkatensis`, `Chamaecyparis spp.`, `Cupressocyparis leylandii`, `Cupressus arizonica`, `Cupressus cashmeriana`, `Cupressus glabra`, `Cupressus lusitanica`, `Cupressus macrocarpa`, `Cupressus sempervirens`, `Cupressus spp.`, `Taxus baccata`, `Thuja occidentalis`, `Thuja orientalis`, `Thuja spp.`

### fastigiate_broadleaf

- Label: Fastigiate/columnar broadleaf
- Basis: Narrow upright broadleaf form for fastigiate cultivars.
- Crown shape: `columnar`
- Default height: `14.0 m`
- Default crown radius: `2.4 m`
- Default trunk radius: `0.085 m`
- Crown base fraction: `0.34`
- Median source diameter: `0.1699 m`
- Source record count: `536`
- Geometry: `models/fastigiate_broadleaf.obj`
- DBH equation: `trunk_radius_m = DBH_m / 2`; mesh top radius is `0.72 * trunk_radius_m`.
- Associated species (3): `Populus nigra var. italica`, `Quercus robur "Fastigiata"`, `Robinia pseudoacacia "Pyramidalis"`

### generic_round_broadleaf

- Label: Generic round broadleaf
- Basis: Fallback broadleaf category when genus-specific form is not assigned.
- Crown shape: `ellipsoid`
- Default height: `11.0 m`
- Default crown radius: `3.5 m`
- Default trunk radius: `0.06 m`
- Crown base fraction: `0.35`
- Median source diameter: `0.0799 m`
- Source record count: `2329`
- Geometry: `models/generic_round_broadleaf.obj`
- DBH equation: `trunk_radius_m = DBH_m / 2`; mesh top radius is `0.72 * trunk_radius_m`.
- Associated species (49): `Acacia dealbata`, `Albizzia julibrissin`, `Broussonetia papyrifera`, `Calycanthus praecox`, `Carya spp.`, `Cercidiphyllum japonicum`, `Cinnamomum camphora`, `Cornus sanguinea`, `Corylus avellana`, `Corylus colurna`, `Cotinus coggygria`, `Cotoneaster spp.`, `Crataegus azarolus`, `Crataegus crus-galli`, `Crataegus laevigata "Paul Scarlet"`, `Crataegus lavallei "Carrierei"`, `Crataegus monogyna`, `Crataegus spp.`, `Elaeagnus angustifolia`, `Eucalyptus spp.`, `Euonymus europaeus`, `Feijoa sellowiana`, `Hippophae rhamnoides`, `Ilex aquifolium`, `Jacaranda`, `Koelreuteria paniculata`, `Laburnum anagyroides`, `Maclura pomifera`, `Melia azedarach`, `Mespilus germanica`, `Nyssa spp.`, `Osmanthus fragrans`, `Ostrya carpinifolia`, `Parrotia persica "Vanessa"`, `Phillyrea latifolia`, `Phillyrea spp.`, `Pistacia lentiscus`, `Pterocarya fraxinifolia`, `Pterocarya spp.`, `Rhamnus alaternus`, `Schinus molle`, `Sorbus aria`, `Sorbus aucuparia`, `Sorbus domestica`, `Sorbus spp.`, `Sorbus torminalis`, `Sterculia platanifolia`, `Syringa vulgaris`, `Tamarix gallica`

### large_round_broadleaf

- Label: Large round/decurrent broadleaf
- Basis: Idealised decurrent broadleaf crown: rounded ellipsoid crown and moderate live-crown base.
- Crown shape: `ellipsoid`
- Default height: `16.0 m`
- Default crown radius: `5.2 m`
- Default trunk radius: `0.1399 m`
- Crown base fraction: `0.34`
- Median source diameter: `0.2706 m`
- Source record count: `50550`
- Geometry: `models/large_round_broadleaf.obj`
- DBH equation: `trunk_radius_m = DBH_m / 2`; mesh top radius is `0.72 * trunk_radius_m`.
- Associated species (99): `Acer campestre`, `Acer japonicum`, `Acer monspessulanum`, `Acer negundo`, `Acer opalus`, `Acer palmatum`, `Acer platanoides`, `Acer pseudoplatanus`, `Acer rubrum`, `Acer saccharinum`, `Acer saccharum`, `Acer spp.`, `Acer tataricum subsp. ginnala`, `Acer x Freemani`, `Aesculus hippocastanum`, `Aesculus pavia`, `Aesculus x carnea`, `Ailanthus altissima`, `Alnus cordata`, `Alnus glutinosa`, `Alnus incana`, `Betula spp.`, `Carpinus betulus`, `Castanea sativa`, `Catalpa bignonioides`, `Celtis`, `Celtis australis`, `Celtis laevigata`, `Celtis occidentalis`, `Fagus sylvatica`, `Fraxinus americana`, `Fraxinus angustifolia`, `Fraxinus angustifolia "Raywood"`, `Fraxinus excelsior`, `Fraxinus ornus`, `Fraxinus spp.`, `Ginkgo biloba`, `Gleditsia Triacanthos Skyline`, `Gleditsia triacanthos`, `Gleditsia triacanthos "Sumburst"`, `Gleditsia triacanthos "inermis"`, `Gleditsia triacanthos spp.`, `Gymnocladus dioicus`, `Liquidambar styraciflua`, `Liriodendron tulipifera`, `Magnolia grandiflora`, `Magnolia spp.`, `Magnolia stellata`, `Magnolia x soulangeana`, `Paulownia tomentosa`, `Platanus Platanor "Vallis Clausa"`, `Platanus occidentalis`, `Platanus orientalis`, `Platanus orientalis varietà cuneata`, `Platanus x acerifolia`, `Populus alba`, `Populus canescens`, `Populus nigra`, `Populus spp.`, `Populus tremula`, `Quercus cerris`, `Quercus crenata`, `Quercus frainetto`, `Quercus ilex`, `Quercus palustris`, `Quercus pedunculata`, `Quercus petraea`, `Quercus phellos`, `Quercus pubescens`, `Quercus rubra`, `Quercus spp.`, `Quercus suber`, `Robinia pseudoacacia`, `Robinia pseudoacacia "Bessoniana"`, `Robinia pseudoacacia "Casque Rouge"`, `Robinia pseudoacacia "Monophilla"`, `Salix alba`, `Salix matsudana "Tortuosa"`, `Salix spp.`, `Salix viminalis`, `Sophora japonica`, `Tilia`, `Tilia americana`, `Tilia cordata`, `Tilia cordata "Erecta"`, `Tilia cordata "Greenspire"`, `Tilia platyphyllos`, `Tilia tomentosa`, `Tilia x Euchlora`, `Tilia x europaea`, `Ulmus glabra`, `Ulmus laevis`, `Ulmus minor`, `Ulmus parvifolia`, `Ulmus pumila`, `Ulmus spp.`, `Zelkova carpinifolia`, `Zelkova serrata`, `Zelkova spp.`

### palm_tuft

- Label: Palm tuft
- Basis: Palm-like tall bare stem with compact crown tuft.
- Crown shape: `tuft`
- Default height: `11.0 m`
- Default crown radius: `2.0 m`
- Default trunk radius: `0.075 m`
- Crown base fraction: `0.74`
- Median source diameter: `0.1499 m`
- Source record count: `220`
- Geometry: `models/palm_tuft.obj`
- DBH equation: `trunk_radius_m = DBH_m / 2`; mesh top radius is `0.72 * trunk_radius_m`.
- Associated species (7): `Chamaerops humilis`, `Jubaea chilensis`, `Musa acuminata`, `Phoenix canariensis`, `Phoenix dactylifera`, `Trachycarpus fortunei`, `Washingtonia filifera`

### pyramidal_conifer

- Label: Pyramidal/excurrent conifer
- Basis: Idealised excurrent conoid tree architecture for conifers.
- Crown shape: `conical`
- Default height: `16.0 m`
- Default crown radius: `3.9 m`
- Default trunk radius: `0.18 m`
- Crown base fraction: `0.24`
- Median source diameter: `0.5097 m`
- Source record count: `1896`
- Geometry: `models/pyramidal_conifer.obj`
- DBH equation: `trunk_radius_m = DBH_m / 2`; mesh top radius is `0.72 * trunk_radius_m`.
- Associated species (26): `Abies alba`, `Abies spp.`, `Calocedrus decurrens`, `Cedrus atlantica`, `Cedrus deodara`, `Cedrus libani`, `Cedrus spp.`, `Cryptomeria japonica`, `Juniperus communis`, `Juniperus spp.`, `Metasequoia glyptostroboides`, `Picea abies`, `Picea pungens`, `Picea spp.`, `Pinus halepensis`, `Pinus mugo`, `Pinus nigra`, `Pinus pinaster`, `Pinus spp.`, `Pinus strobus`, `Pinus sylvestris`, `Pinus wallichiana`, `Pseudotsuga menziesii`, `Sequoia sempervirens`, `Sequoiadendrom giganteum`, `Taxodium distichum`

### small_round_broadleaf

- Label: Small ornamental or fruit broadleaf
- Basis: Compact round crown for smaller fruit, ornamental, and shrub-like urban trees.
- Crown shape: `rounded`
- Default height: `8.5 m`
- Default crown radius: `2.8 m`
- Default trunk radius: `0.0684 m`
- Crown base fraction: `0.36`
- Median source diameter: `0.1369 m`
- Source record count: `13716`
- Geometry: `models/small_round_broadleaf.obj`
- DBH equation: `trunk_radius_m = DBH_m / 2`; mesh top radius is `0.72 * trunk_radius_m`.
- Associated species (69): `Arbutus andrachne`, `Arbutus unedo`, `Buxus balearica`, `Buxus sempervirens`, `Ceratonia siliqua`, `Cercis siliquastrum`, `Citrus limon`, `Citrus spp.`, `Clerodendrum`, `Cydonia oblonga`, `Diospyros kaki`, `Diospyros lotus`, `Diospyros virginiana`, `Eriobotrya japonica`, `Ficus carica`, `Hibiscus rosa-sinensis`, `Juglans nigra`, `Juglans regia`, `Lagerstroemia indica`, `Laurus nobilis`, `Ligustrum japonicum`, `Ligustrum lucidum`, `Ligustrum spp.`, `Malus "Golden Hornet"`, `Malus "Hopa"`, `Malus "Red Sentinel"`, `Malus "profusion"`, `Malus courtarou x coccinella`, `Malus domestica`, `Malus floribunda`, `Malus spp.`, `Morus alba`, `Morus alba "Platanifolia"`, `Morus nigra`, `Morus spp.`, `Nerium oleander`, `Olea europaea`, `Olea fragrans`, `Photinia spp.`, `Pittosporum tobira`, `Prunus Padus`, `Prunus amygdalus`, `Prunus armeniaca`, `Prunus avium`, `Prunus avium "Plena"`, `Prunus cerasifera`, `Prunus cerasifera "Nigra"`, `Prunus cerasifera var. pissardii`, `Prunus domestica`, `Prunus laurocerasus`, `Prunus lusitanica`, `Prunus persica`, `Prunus serrulata "Amanogawa"`, `Prunus serrulata "Kanzan"`, `Prunus spp.`, `Prunus subhirtella "Autumnalis Rosea"`, `Prunus subhirtella "Autumnalis"`, `Punica granatum`, `Pyrus calleryana var. chanticleer`, `Pyrus communis`, `Pyrus pyraster`, `Pyrus spp.`, `Sambucus nigra`, `Sambucus spp.`, `Viburnum tinus`, `Vitex agnus castus`, `Yucca`, `Yucca gloriosa`, `Ziziphus sativa`

### umbrella_pine

- Label: Umbrella pine
- Basis: Mediterranean stone pine umbrella canopy with raised crown base.
- Crown shape: `umbrella`
- Default height: `14.0 m`
- Default crown radius: `5.3 m`
- Default trunk radius: `0.3024 m`
- Crown base fraction: `0.58`
- Median source diameter: `0.6048 m`
- Source record count: `3466`
- Geometry: `models/umbrella_pine.obj`
- DBH equation: `trunk_radius_m = DBH_m / 2`; mesh top radius is `0.72 * trunk_radius_m`.
- Associated species (1): `Pinus pinea`

### weeping_broadleaf

- Label: Weeping broadleaf
- Basis: Low rounded proxy for pendulous cultivars, preserving larger lateral spread.
- Crown shape: `ellipsoid`
- Default height: `9.0 m`
- Default crown radius: `3.8 m`
- Default trunk radius: `0.095 m`
- Crown base fraction: `0.42`
- Median source diameter: `0.1799 m`
- Source record count: `125`
- Geometry: `models/weeping_broadleaf.obj`
- DBH equation: `trunk_radius_m = DBH_m / 2`; mesh top radius is `0.72 * trunk_radius_m`.
- Associated species (5): `Betula pendula`, `Morus alba "Pendula"`, `Pyrus salicifolia "Pendula"`, `Salix babylonica`, `Sophora japonica "Pendula"`


## Reference Basis

- [Crown botany: excurrent conoid and decurrent round crown habits](https://en.wikipedia.org/wiki/Crown_%28botany%29)
- [Tree crown measurement: crown spread, crown form, and crown-volume simplification](https://en.wikipedia.org/wiki/Tree_crown_measurement)
- [Tree allometry: diameter relationships used for idealised tree dimensions](https://en.wikipedia.org/wiki/Tree_allometry)
- [Common landscape tree shape categories: columnar, pyramidal, globe, vase, spreading, weeping](https://www.bhg.com/gardening/trees-shrubs-vines/trees/selecting-trees-by-shape/)
