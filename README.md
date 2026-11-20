# Video Transformer Pro

A local Flask + FFmpeg video editor designed to run on Android through Termux or on a desktop.

## Features

- Browser video preview before processing
- Trim start/end times
- Multiple video filters: Natural, Cinematic, Vivid, Vintage, B&W, Warm and Cool
- Merge up to five clips
- Optional text watermark
- FFmpeg-based processing progress
- 480p, 720p, 1080p and 4K output
- 16:9, 9:16 and 1:1 aspect ratios
- Download history

## Requirements

- Python 3.10+
- Flask (install with `pip install -r requirements.txt`)
- FFmpeg and FFprobe available on PATH

## Run on Android (Termux)

Install Termux packages:

```bash
pkg update
pkg install python ffmpeg
pip install -r requirements.txt
```

Start the local server:

```bash
python app.py
```

Open `http://127.0.0.1:5000` in the phone browser.

## Notes

- The server binds to localhost only. It is intended for local use, not direct public exposure.
- Upload limit is 500 MB per request; up to five clips can be merged.
- For each clip, End time `0` means use the full clip. A chosen end time longer than a clip is clamped to that clip's duration.
- 4K export can be slow and resource-intensive on mobile devices.
- Exported videos are stored in `outputs/`; upload and temporary files are removed after processing.
- History metadata is stored in `history.json` and is excluded from Git.
- Only edit videos you own or are authorized to use.
