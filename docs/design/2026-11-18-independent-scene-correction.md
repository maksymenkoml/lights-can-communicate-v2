# Independent scene brightness correction

Issue #44. Decided by the owner on 2026-11-18.

## Intent

Light Studio keeps a brightness correction per lamp for each configuration: with
Sync (`scale`) and without Sync (`sceneScale`). A row without `sceneScale`
inherits `scale`, but nothing shows the inheritance. A without-Sync value then
changes with the Sync value, which looked like the two configurations were not
stored separately.

The owner chose to remove the link instead of showing it: the without-Sync
correction is always the lamp's own value.

## Goals

- Every present row has its own `sceneScale` after validation. A Sync
  correction change never changes the without-Sync correction.
- Light output stays the same at the change: a missing `sceneScale` becomes a
  copy of the current `scale`, which is the value scenes use today.
- The adapter, the Hue scene proxies and Light Studio read the same numbers.

## Non-goals and anti-goals

- No new Light Studio control, label or state. Nothing visual changes.
- No layout version change and no migration step. A rollback to an older
  release must still read the files.
- The Sync correction (`scale`) is not changed.

## Approaches

- **Chosen: fill `sceneScale` in `validate_layout`.** Every read and every write
  passes it, so the adapter, the proxies and Light Studio get the same value,
  and a saved file carries it from the next write. `sceneScale` already exists
  as an optional version 5 field, so older releases read the result.
- Copy only in Light Studio: rejected. Files that Light Studio has not saved
  would keep the inheritance in the adapter, so the two could disagree.
- New layout version with a required field: rejected. It needs a migration and
  breaks rollback, and the optional field is enough.
- Keep the link and label it ("from Sync" plus a "Use Sync value" action): the
  owner rejected it because it adds states to understand.

## Design

- `bridge/desk_layout.py`: `validate_layout` stores
  `sceneScale = sceneScale if given, else scale` on every row.
  `LayoutStore.scene_scale` reads `sceneScale`.
- `studio/static/app.js`: a loaded lamp's `sceneScale` is always a number (the
  row value, else its `scale`), `pack` always writes it, and `corr` reads it
  directly. A new lamp starts with equal corrections in both configurations.
- Docs (`core/README.md`, `studio/README.md`, the lamp-controls design) describe
  `sceneScale` as stored on every row; a file without it gets a copy of
  `scale` when it loads.

## Left to the builder

- Test updates for the old "defaults are not stored" and "falls back to
  scale" contracts.
