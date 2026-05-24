# Composer Bridge Setup

The pipeline renders assembly step images by talking to a local FastAPI server that controls SolidWorks Composer. This guide walks through two modes:

- **Folder mode** — you export PNGs from Composer manually, server serves them (works immediately, no COM needed)
- **COM mode** — server controls Composer automatically via Windows COM API (fully automated, requires `pywin32`)

---

## 1. Install bridge dependencies

```powershell
pip install fastapi uvicorn pywin32
```

---

## 2. Choose a mode

### Folder mode (start here)

1. Open your SolidWorks assembly in Composer (File → Open → select your `.sldasm`)
2. Set up a view for each assembly step you want to photograph
3. Export each view as a PNG: **File → Publish → Images** (or right-click view → Export Image)
4. Name each file to match its step ID — the step IDs are the ones printed by Stage 1 of the pipeline (e.g. `step_1.png`, `step_2.png`)
5. Drop all PNGs into `assets/composer_renders/`

Set in your `.env`:
```
BRIDGE_MODE=folder
RENDER_DIR=assets/composer_renders
```

### COM mode (automated)

First, find your Composer COM ProgID:
```powershell
python -m composer.discover_com
```

If it finds one (e.g. `SWComposerLib.Application`), add to `.env`:
```
BRIDGE_MODE=com
COMPOSER_PROGID=SWComposerLib.Application
SMG_PATH=C:\path\to\your\assembly.smg
```

You'll need an `.smg` file — export it from Composer: File → Save As → `.smg`.

> **Note:** COM method names in `composer/server.py` are best-guess based on the Composer API pattern.
> If you get attribute errors, check the Composer API docs for your version and update the
> `_com_list_views`, `_com_render`, and `_com_author_view` functions accordingly.

---

## 3. Start the bridge server

```powershell
python -m uvicorn composer.server:app --host 127.0.0.1 --port 8000
```

Leave this running while you run the pipeline.

---

## 4. Run the pipeline

```powershell
python pipeline.py --eco eco.json --instructions instructions.txt
```

No `--smg` flag needed. Stage 5 will connect to the bridge at `http://localhost:8000` and pull images for all steps that need them.

---

## Naming convention for images

The pipeline maps `step_id → view_id → filename`. To know your step IDs, run:

```powershell
python pipeline.py --eco eco.json --instructions instructions.txt
```

Stage 1 prints all parsed steps. Use those IDs as your PNG filenames (or Composer view names in COM mode).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Stage 5: "Composer bridge not reachable" | Bridge server isn't running — start it first |
| 404 on `/render/{view_id}` in folder mode | PNG `{view_id}.png` missing from `RENDER_DIR` |
| COM: "no ProgID matched" | Run `python -m composer.discover_com`, set `COMPOSER_PROGID` |
| COM: AttributeError on `.Views` or `.Activate()` | Check your Composer version's API docs and update method names in `composer/server.py` |
| Images not showing in PDF | Check `assets/composer_renders/` — PNGs must exist before Stage 7 runs |
