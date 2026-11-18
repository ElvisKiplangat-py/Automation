# Natural code typing (Kali Linux)

`editor_activity.py` reads prepared source files and types their content into new editor files using simulated keyboard events. It does not paste the whole file. Source files are read-only to the script; output files are created under `replayed_code/` and existing outputs are not overwritten.

## Install

```bash
sudo apt update
sudo apt install wmctrl xdotool python3-tk python3-dev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Make sure Cursor and VS Code are installed and their `cursor` and `code` launchers are on `PATH`. Run from a graphical X11 desktop terminal.

## Type a source file

For the active `app1.py` source:

```bash
python editor_activity.py --once --source app1.py --editor "VS Code"
```

This creates a fresh output such as `replayed_code/001_app1.py`, opens it in VS Code, and types the source line by line. The source file remains unchanged. To type into Cursor, use `--editor Cursor`.

Supply multiple files in the order they should be typed:

```bash
python editor_activity.py --once \
  --source code_to_replay/01_intro.py \
  --source code_to_replay/02_models.py \
  --source code_to_replay/03_runner.py
```

The default pace targets about 30 hours per 10,000 lines. It scales character delays and pauses to the source length, while still varying delays, gradually slowing up to 2.5×, and occasionally correcting a temporary typo with Backspace. Simulated key delay never drops below 50 ms, so actual time may exceed the target for sources whose calculated pace would otherwise be too fast. The script prints the target time estimate before typing. Adjust `--target-hours-per-10000-lines` to change the overall pace; the delay and pause options shape the rhythm used to scale that target.

During source replay, keystrokes target the selected editor window, and the script checks that the editor remains active before each line. If focus changes to another app, replay stops with an error. Pointer movement and optional clicks are disabled during code replay. Targeted synthetic X11 events may be ignored by some applications; if VS Code ignores them, keep VS Code focused and use a foreground-input approach with focus checks instead.

You can queue multiple source files in an explicit order. With `--once`, each source is typed once and the script exits after the list is complete:

```bash
python editor_activity.py --once \
  --source app1.py \
  --source app2.py \
  --source app3.py
```

Each source is typed into its own newly created file under `replayed_code/`, named with its queue position and source filename. Existing output files are left untouched. When `--once` is omitted, the script repeats the entire source list indefinitely, creating a new output copy each cycle.

After typing a source, the script moves directly to the next source in the queue. In code replay mode it does not continue mouse movement or clicking after typing.

```bash
python editor_activity.py --once --source app1.py --typo-chance 0.002 --slowdown-factor 2.5
```

Thousands of lines can take a long time at human-like speeds. Press **Ctrl+C** to stop. The script operates the visible desktop, so leave the mouse and keyboard alone while it runs.
