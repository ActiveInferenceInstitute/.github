# A Reproducible Repository Readiness Standard for the Active Inference Institute

## Source

- [Abstract](00_abstract.md)
- [The readiness model](01_model.md)
- [Replaying a readiness assessment](02_replay.md)
- [Publication boundary and limits](03_publication.md)
- [References](99_references.md)

The [render configuration](config.yaml) enables PDF and HTML. The [bibliography](references.bib) records the sources cited in the text. The [preamble](preamble.md) supplies PDF typesetting.

## Render and validate

Use a clean checkout of this repository and a separate checkout of the
[pinned docxology template](https://github.com/docxology/template/tree/87704feae0a4490ef7dad9c931d268e8c30273c2).
Install Git, uv, Pandoc, XeLaTeX, and the dependencies in the
[template setup guide](https://github.com/docxology/template/blob/87704feae0a4490ef7dad9c931d268e8c30273c2/docs/guides/startup-and-setup.md).
Clone this repository into a visible directory such as `org-readiness`,
then run from that checkout root. The template validator uses the directory
name when discovering the combined PDF.

```bash
source_checkout="$(pwd -P)"
slug="$(basename "$source_checkout")"
renderer_checkout="$(mktemp -d)/template"
git clone --filter=blob:none --no-checkout https://github.com/docxology/template.git "$renderer_checkout"
git -C "$renderer_checkout" sparse-checkout set infrastructure scripts projects
git -C "$renderer_checkout" checkout --detach 87704feae0a4490ef7dad9c931d268e8c30273c2
cd "$renderer_checkout"
uv sync --locked
mkdir -p projects/working
ln -s "$source_checkout" "projects/working/$slug"
uv run --no-sync python scripts/pipeline/stage_03_render.py --project "working/$slug"
uv run --no-sync python scripts/pipeline/stage_04_validate.py --project "working/$slug"
```

Check `output/pdf/` and `output/web/` in the source checkout for the assembled
paper. The validation verdict and provenance are recorded in `output/reports/`.
