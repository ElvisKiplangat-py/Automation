# Natural code typing (Kali Linux and WSL)

`editor_activity.py` reads prepared source files and reproduces their content in new editor files. On Linux/X11 it uses simulated keyboard events; in WSL with Windows VS Code it pastes one character at a time to preserve exact content. Source files are read-only to the script; output files are created under `replayed_code/` and existing outputs are not overwritten.

## Install

```bash
sudo apt update
sudo apt install wmctrl xdotool python3-tk python3-dev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Make sure Cursor and VS Code are installed and their `cursor` and `code` launchers are on `PATH`. On Linux, run from a graphical X11 desktop terminal.

## WSL with Windows VS Code

When this repository is opened in VS Code using **Reopen Folder in WSL**, run the script in its WSL terminal. WSL mode uses the Windows input bridge, so it does not require `wmctrl`, `xdotool`, or an Xauthority file. It supports VS Code source replay and pointer movement; Cursor is not supported in this mode. Source is pasted one character at a time at a simulated pace. The pointer follows smooth, curved paths inside the VS Code window. Pass `--click` for an occasional click in the central editor area; clicks run only in movement-only sessions or after source replay finishes. Manual pointer movement pauses automation for 5 seconds by default; `--pointer-user-pause SECONDS` changes that interval. Switching to another app pauses source replay and pointer movement until VS Code is foregrounded again; the session clock pauses too. Movement-only sessions work when `--source` is omitted.

```bash
python editor_activity.py --once --source app1.py --editor "VS Code"
```

The script opens a new output file under `replayed_code/`, focuses VS Code's editor group, and pastes the source one character at a time. Keep the VS Code window visible. The script checks the saved output against the source when replay finishes.

## Type a source file

For the active `app1.py` source:

```bash
python editor_activity.py --once --source app1.py --editor "VS Code"
```

This creates a fresh output such as `replayed_code/001_app1.py`, opens it in VS Code, and reproduces the source line by line. The source file remains unchanged. To use Cursor on Linux/X11, pass `--editor Cursor`.

Supply multiple files in the order they should be typed:

```bash
python editor_activity.py --once \
  --source code_to_replay/01_intro.py \
  --source code_to_replay/02_models.py \
  --source code_to_replay/03_runner.py
```

The default pace targets about 30 hours per 10,000 lines. It scales character delays and pauses to the source length, while still varying delays, gradually slowing up to 2.5×, and occasionally correcting a temporary typo with Backspace. Simulated key delay never drops below 50 ms, so actual time may exceed the target for sources whose calculated pace would otherwise be too fast. The script prints the target time estimate before typing. Adjust `--target-hours-per-10000-lines` to change the overall pace; the delay and pause options shape the rhythm used to scale that target.

During Linux/X11 source replay, keystrokes target the selected editor window, and the script checks focus before each line. The pointer occasionally moves inside the editor between lines; it does not click. In WSL, the pointer moves inside the Windows VS Code window between pasted chunks and during pauses, without clicking.

You can queue multiple source files in an explicit order. With `--once`, each source is typed once and the script exits after the list is complete:

```bash
python editor_activity.py --once \
  --source app1.py \
  --source app2.py \
  --source app3.py
```

Each source is typed into its own newly created file under `replayed_code/`, named with its queue position and source filename. Existing output files are left untouched. When `--once` is omitted, the script repeats the entire source list indefinitely, creating a new output copy each cycle.

After reproducing a source, the script continues pointer movement in WSL until the configured session duration ends. Linux/X11 source replay exits after typing.

```bash
python editor_activity.py --once --source app1.py --typo-chance 0.002 --slowdown-factor 2.5
```

Thousands of lines can take a long time at human-like speeds. Press **Ctrl+C** to stop. The script operates the visible desktop, so leave the mouse and keyboard alone while it runs.
