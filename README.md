# Squint

A daily zoom-out picture puzzle. One picture, six looks.

## Folder layout

- `index.html`: the game
- `answers.json`: every valid guess (powers the autocomplete)
- `puzzles/schedule.json`: which puzzle folder plays on which day (first entry = puzzle #1)
- `puzzles/<folder>/`: one puzzle: 7 images + `game.json`
- `tools/crop-picker.html`: makes new puzzles

## Run it locally

The game loads JSON files, so it must be served, not double-clicked.
In VS Code, right-click `index.html` and choose "Open with Live Server".
Add `?day=2` to the URL to preview any puzzle without affecting your stats.

## Add a puzzle

1. Open `tools/crop-picker.html`, load a photo, frame the six levels and the full picture.
2. Fill in the ID, answer, hint, source and license, then download the zip.
3. Copy the `squint-puzzle-<id>` folder into `puzzles/`.
4. Add the folder name to `puzzles/schedule.json` in the position for its day.
5. Make sure the answer is in `answers.json`.
6. Keep `squint-puzzle-<id>-record.json` somewhere private (it has the plain answer and license).

## Settings

At the top of the script in `index.html`: `SITE_URL` (shown in share text)
and `LAUNCH` (the date of puzzle #1).
