# Idealised Tree Category Models

This folder contains category-level parametric tree models used by the tree stage. Species from the Florence OpenData DBF are mapped to these categories in `docs/assets/data/florence_opendata/trees_diameter/species_category_mapping.json`.

Categories intentionally group species with similar CFD-scale crown architecture rather than creating one geometry archetype per species. The models are low-poly idealised OBJ assets for QA and CFD surface preparation.

Generated review artifacts:

- `category_models_preview.html`: self-contained 3D canvas preview of every category OBJ model. Species names in the category panels are clickable and fetch up to three reference images with source links for visual verification. Exact binomial species first use Wikidata scientific-name taxa and Wikimedia Commons taxon categories; genus-only and `spp.` source values are labelled as broader references.
- `category_model_catalog.md`: category parameters, DBH scaling rules, and all mapped species per category.

Regenerate them from the repository root with:

```bash
python3 tools/build_tree_category_catalog.py
```

The current category set follows common tree-form and forestry abstractions: rounded/decurrent broadleaf crowns, conical/excurrent conifers, columnar or fastigiate trees, weeping trees, umbrella pines, and palm tufts. Category defaults use DBH-derived source medians where available, with broad allometric fallbacks for height, crown radius, and trunk radius.

Reference basis:

- Crown architecture: excurrent crowns tend toward conoid forms; decurrent crowns tend toward rounded forms.
- Crown measurement: crown spread, crown thickness, crown volume, and crown shape are standard forestry descriptors.
- Tree allometry and DBH: diameter is commonly used to estimate harder-to-measure tree dimensions.
- Urban tree shape classes: columnar, pyramidal, globe/round, fastigiate, weeping, and spreading forms are common landscape categories.
