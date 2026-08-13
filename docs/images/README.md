# Screenshots

GitHub renders the README in a column about **900 px wide**. Anything wider is
scaled down, and anything taller than roughly **700 px** pushes the text that
follows off the screen. Width is easy; height is the constraint worth planning
around.

Capture at 2× (a Retina screen already does) and let GitHub scale it down — the
`width` attribute in the README controls the displayed size.

| File | Contents | Target size |
|---|---|---|
| `card-back.png` | The hero shot at the top. The back of a **noun** card, cropped to end after the second example. | ~1000 × 650 |
| `terminal.png` | A run of `anki-vocab "el perro"`, from the first INFO line to the last row of the preview. | ~1280 × 700 |
| `card-front.png` | The front: term, sense hint, part of speech. Mostly empty space, so crop tight. | ~640 × 300 |
| `card-back-full.png` | The whole back, shown beside the front in the same row. | ~640 × 900 |

Anki's card preview gives the cleanest shot: *Browse → select a note → Preview*.
On macOS, `Cmd+Shift+4` then `Space` captures a whole window, drop shadow
included. Dark mode works too — the CSS supports it.

## The back is very tall

It is, especially for a verb: term, IPA, audio, definition, two examples,
construction, false friend, usage note, forms and a ten-row conjugation table.
Four ways out, in the order worth trying.

**Pick a shorter word for the hero shot.** A noun with a gender, a plural and a
false friend — *la planta*, *el estante*, *constipado* — shows off most of the
design in half the height. Save the verb for a second image where the
conjugation table is the point.

**Crop it.** The hero image does not have to document every field; the table in
the README already does. Ending it after the second example reads far better.

**Split it into two columns.** Two images side by side halve the height, and no
composition tool is needed — GitHub lays out two `<img>` tags in the same
paragraph next to each other:

```bash
uv run --with pillow --no-project python - <<'EOF'
from PIL import Image
img = Image.open("docs/images/card-back-full.png")
w, h = img.size
half, overlap = h // 2, 40          # overlap so nothing is lost at the seam
img.crop((0, 0, w, half + overlap)).save("docs/images/card-back-1.png")
img.crop((0, half - overlap, w, h)).save("docs/images/card-back-2.png")
EOF
```

```html
<p align="center">
  <img src="docs/images/card-back-1.png" width="380">
  <img src="docs/images/card-back-2.png" width="380">
</p>
```

**Or widen the Anki window before capturing.** The definition and examples are
capped at `34em`, but the section boxes stretch, so a wider window packs more
into each line and takes a few hundred pixels off the total height.

## Checking a size

```bash
sips -g pixelWidth -g pixelHeight docs/images/card-back.png
```
