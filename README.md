# media-glicko2

A Tkinter desktop app for **pairwise media preference ranking** powered by **Glicko-2**.

Each image is treated like a “player,” each comparison is a “match,” and ratings are updated at the end of each session. The app is designed for iterative curation workflows where you repeatedly compare, rank, prune, and add images over time.

## Features

- **Glicko-2 ratings per image**
  - `R` (rating): estimated preference strength
  - `RD` (rating deviation): uncertainty (lower = more confidence)
  - `σ` (volatility): how much the item’s true strength appears to change
- **Efficient session pairing**
  - Random disjoint pairs per session
  - Each image appears at most once per session
  - Scales better than full round-robin
- **Persistent ratings via filename metadata**
  - Ratings are stored in filename prefixes and loaded on startup
  - No reset to defaults when re-opening the same folder
- **Human-in-the-loop interaction**
  - Mouse click left/right image to pick winner
  - Keyboard shortcuts for speed
  - Draw/tie support
  - Undo last comparison
- **Continuous workflow**
  - Session ends → ratings update → files renamed → next session starts immediately
  - Folder remains loaded

## Rating metadata in filenames

The app writes metadata as a **prefix**:

```text
[G2_R1623.4_RD121.7_S0.0598] filename.jpg
```

Why prefixing helps:

- Filesystem sorting reflects ranking order
- Ratings are visible directly in file explorers
- The app can parse existing metadata and continue from prior state

## Supported media formats

- **Images/GIF**: `.jpg`, `.jpeg`, `.png`, `.bmp`, `.gif`, `.webp`
- **Video**: `.mp4`, `.mov`, `.mkv`, `.webm`, `.avi`, `.m4v`

Animated GIFs and videos are played in a loop while you compare a pair.

## Requirements

- Python 3.10+
- [Pillow](https://pypi.org/project/Pillow/)
- [ImageIO](https://pypi.org/project/imageio/) (for video frame decoding)
- Tkinter (usually included with standard Python installers)

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run

From the project directory:

```bash
python media-glicko2.py
```

## Usage

1. Launch the app.
2. Click **Choose Folder** and select a folder with supported images.
3. Compare image pairs:
   - Click left/right image, or use keyboard shortcuts below.
4. At session end, ratings are batch-updated and files are renamed with fresh metadata.
5. A new session starts automatically with the same folder.

## Controls

- **Mouse**
  - Click left image → left wins
  - Click right image → right wins
- **Keyboard**
  - `←` left wins
  - `→` right wins
  - `↑` draw/tie
  - `↓` undo last comparison
  - `Esc` exit

## How the session model works

- One session = one Glicko-2 rating period
- Pairing is randomized and disjoint each session
- If odd number of images, one sits out that session
- Updates are applied in batch at session completion

This aligns with Glicko-2’s period-based design while remaining practical for larger sets.

## Interpreting Glicko-2 values in this app

- `R` increases when an image wins against expected or stronger opponents.
- `RD` decreases as an image accumulates comparisons (more certainty).
- `RD` increases for inactivity (less certainty over time).
- `σ` tends to increase when results are inconsistent or preference appears to shift.

## Dynamic dataset behavior

This project works well for living datasets where you periodically:

- remove poor performers (often low `R` with low `RD` confidence), and
- add new images.

Because of this, rankings are relative to the **current pool** and naturally track a moving frontier of best images.

## Limitations / notes

- Updates are batch-applied at session end; if you close mid-session, that session’s uncommitted comparisons are not persisted.
- Video playback requires `imageio` and a compatible local decoder stack.
- Very long media files are sampled to a capped number of frames to keep UI responsive.

## File overview

- `media-glicko2.py` — main app, Glicko-2 logic, media loading/playback, pairing, UI, undo/draw handling, filename persistence.
- `tests/test_media_glicko2.py` — unit tests for media loading helpers and supported extension behavior.

## License

Add a license file if you intend to distribute this project.
