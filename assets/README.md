# Brand assets

## The mark

A **signal hoist**: two flags bent on one halyard.

The lead flag carries the white centre of the **Blue Peter** — the maritime signal flag for the
letter **P**, flown when a vessel is about to sail to mean *"all persons report aboard"*. It is
literally the letter the project is named for, and its meaning is what a parley is: everyone who
is coming, come now, we are starting.

The second flag is the answer. Two parties, one line.

| File | Use |
|---|---|
| `icon.svg` | The mark. Use this wherever it will render above ~48 px. |
| `icon-small.svg` | The small cut, for 32 px and below. The Blue Peter's white centre is dropped, because at favicon size that detail turns to mush and costs more legibility than the meaning is worth. |
| `icon-16/32/64/128/256/512.png` | Rasterised. The 16 and 32 come from the small cut, the rest from the full mark. |
| `favicon.ico` | All five sizes in one file, each from the cut drawn for it. |
| `social-preview.png` | 1280×640, for GitHub → Settings → Social preview. |
| `deck-*.png` | Screenshots of the Deck on the bundled fixture. |

## Colour

Taken from the Deck's stylesheet rather than chosen separately, so the identity and the product
cannot drift apart.

| | |
|---|---|
| Harbour teal (light ground) | `#0a6e73` |
| Harbour teal (dark ground) | `#3fc8c8` |
| Field gradient | `#0d8a90` → `#075458` |
| Ink | `#0b1416` |

## Regenerating

The screenshots are taken against `parley/hub/deck/index.html?fixture=1`, which needs no Hub
running. Two screenshot-only adjustments are made to a *copy* of the deck, never to the shipped
files: the boot theme is pinned so light and dark can be captured deterministically, and the
skip-link is hidden because headless Chromium focuses it and it then paints over the session bar.

Rasterising the SVGs needs any headless browser. Note that `favicon.ico` is assembled by writing
the ICO container directly — Pillow's writer resizes a single source image and will silently
drop every entry but one.
