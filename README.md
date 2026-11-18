# Squint

A daily zoom-out picture puzzle. One picture, six looks.

## Folder layout

- `index.html`: the game
- `answers.json`: every valid guess (powers the autocomplete)
- `similar.json`: rules for "close enough" and "close" guesses
- `og-image.png`: the image shown when your link is shared
- `favicon.svg`, `favicon-32.png`, `apple-touch-icon.png`: site icons
- `puzzles/schedule.json`: which puzzle folder plays on which day (first entry = puzzle #1)
- `puzzles/<folder>/`: one puzzle: 7 images + `game.json`
- `tools/crop-picker.html`: makes new puzzles

## Add a puzzle

1. Open `tools/crop-picker.html`, load a photo, frame the six levels and the full picture.
2. Fill in the ID, answer, any extra accepted answers, hint, source and license, then download the zip.
3. Copy the `squint-puzzle-<id>` folder into `puzzles/`.
4. Add the folder name to `puzzles/schedule.json` in the position for its day.
5. Keep `squint-puzzle-<id>-record.json` somewhere private (it has the plain answer and license).

## How guesses are judged (similar.json)

- `same`: names that mean the same thing. Any of them is correct.
- `kinds`: a general name and its specific kinds. Either way round is correct ("Owl" for "Barn owl").
  Two different kinds of the same thing are "Close" ("Snowy owl" for "Barn owl").
- `broad`: very general words and loose families. Never correct, but marked "Close" ("Bird" for "Barn owl").
- `close`: things people mix up. Marked "Close" ("Alligator" for "Crocodile").
- Per puzzle: the crop picker's "Also accept" field adds extra correct answers for that puzzle only.

Typed guesses ignore capitals, accents, punctuation, a leading "the" and plural endings.

## Settings

- At the top of the script in `index.html`: `SITE_URL` (shown in share text) and `LAUNCH` (the date of puzzle #1).
- In the `<head>` of `index.html`: the three lines marked `SITE` hold your site's address for link previews.
