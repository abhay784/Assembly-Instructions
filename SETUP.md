# Assembly Instructions AI Agent — Setup & Usage Guide

This pipeline automatically improves assembly instructions by analyzing engineering changes and using AI to revise affected steps. It can work with SolidWorks models directly or with pre-computed ECO (Engineering Change Order) files.

## Installation

```bash
pip install -r requirements.txt
```

## Quick Start

### On Windows (with SolidWorks installed)

For the **full experience** with automatic image rendering:

```bash
python pipeline.py \
  --before-model old_assembly.sldasm \
  --after-model new_assembly.sldasm \
  --instructions instructions.pdf
```

The pipeline will:
1. Extract text from your PDF
2. Compare the two assembly files using SolidWorks
3. Identify what changed (parts, dimensions, mates)
4. Use AI to revise instruction text for affected steps
5. Generate a new PDF with improved instructions

### On Mac or Without SolidWorks

You have two options:

**Option A: If you already have an ECO file**
```bash
python pipeline.py \
  --eco eco.json \
  --instructions instructions.pdf
```

**Option B: Generate ECO on Windows, then run on Mac**
1. On your Windows machine, run:
   ```bash
   python3 << 'EOF'
   from eco.synthesizer import synthesize_ecos
   import json
   ecos = synthesize_ecos("old_assembly.sldasm", "new_assembly.sldasm")
   with open("eco.json", "w") as f:
       json.dump([e.model_dump() for e in ecos], f, indent=2)
   EOF
   ```

2. Copy `eco.json` to your Mac
3. Run the pipeline:
   ```bash
   python pipeline.py --eco eco.json --instructions instructions.pdf
   ```

## Instruction File Formats

The `--instructions` argument accepts:
- **`.txt` files** — Plain text extracted from documents
- **`.pdf` files** — Automatically extracts text from all pages
- **`.json` files** — Structured instruction format (advanced)

## Output Files

The pipeline creates an `output/` directory with:

```
output/
├── MyAssembly_PDF.pdf          ← Final assembly instructions document
├── MyAssembly_diff.json        ← Detailed change summary for each step
└── review_required.json        ← Steps flagged for engineer review
```

## Review & Approval Workflow

If steps are flagged during the eval gate, they appear in `review_required.json`:

```json
{
  "flagged_steps": [
    {
      "step_id": "step_001",
      "flags": [
        {"flag_type": "ambiguous_revision", "message": "Text revision unclear"}
      ],
      "approved": false
    }
  ]
}
```

**To approve and finalize:**

1. Edit `review_required.json` and set `"approved": true` for steps you accept
2. Run:
   ```bash
   python pipeline.py --publish <run_id>
   ```
   (The run_id was printed when you first ran the pipeline)

3. Final PDF is regenerated with your approvals applied

## Environment Configuration

Create a `.env` file in the project root:

```bash
# LLM Backend (required)
LLM_BACKEND=claude
ANTHROPIC_API_KEY=your_key_here
CLAUDE_MODEL=claude-opus-4-7

# Or use vLLM instead:
# LLM_BACKEND=vllm
# VLLM_BASE_URL=http://localhost:8001/v1
# VLLM_MODEL=meta-llama/Llama-2-70b-hf

# SolidWorks Composer API (optional, for Windows image rendering)
SOLIDWORKS_API=http://localhost:8000
```

## Full Command Reference

```bash
python pipeline.py \
  --before-model old_assembly.sldasm \
  --after-model new_assembly.sldasm \
  --instructions instructions.pdf \
  --smg my_scene.smg \
  --document-id MyAssembly \
  --output-dir output
```

| Option | Required | Description |
|--------|----------|-------------|
| `--eco` | ✓* | Path to ECO JSON file |
| `--before-model` | ✓* | Path to before SolidWorks model (for auto ECO synthesis) |
| `--after-model` | ✓* | Path to after SolidWorks model (for auto ECO synthesis) |
| `--instructions` | ✓ | Path to .txt, .pdf, or .json instruction file |
| `--smg` | - | Path to SolidWorks Composer .smg file (for image rendering) |
| `--document-id` | - | Document identifier (default: "document") |
| `--output-dir` | - | Output directory (default: "output") |
| `--run-id` | - | Resume a specific run (skip completed stages) |
| `--publish` | - | Re-render PDF after review (pass the run_id) |

*Either provide `--eco` OR both `--before-model` and `--after-model`

## Pipeline Stages

The pipeline runs 8 stages automatically:

| Stage | Name | What it does |
|-------|------|-------------|
| 0 | ECO Ingest | Reads model changes or ECO file |
| 1 | Instruction Parser | Parses raw instruction text into structured steps |
| 2 | Change Mapper | Identifies which steps are affected by changes |
| 3 | Agent Planner | AI decides what actions to take per step |
| 4 | Text Revision | AI rewrites affected instruction text |
| 5 | Image Renderer | Re-renders assembly images (requires Composer) |
| 6 | Eval Gate | Quality checks and flags issues for review |
| 7 | Doc Stitcher | Assembles final HTML document |
| 8 | PDF Generator | Exports PDF and review files |

Each stage result is cached in `.pipeline_state/`. If you re-run with the same `--run-id`, skipped stages are loaded from cache.

## Troubleshooting

### "Composer bridge at SOLIDWORKS_API is not reachable"
You specified `--smg` but the FastAPI bridge on Windows isn't running. Either:
- Start the Composer service on your Windows machine, or
- Remove `--smg` to skip image rendering (steps will be flagged for manual review)

### "pypdf not installed"
The pipeline tried to extract text from a PDF but `pypdf` is missing:
```bash
pip install pypdf
```

### No ECOs generated
Ensure your `.sldasm` files have custom properties (part number, description, etc.) set in SolidWorks. The pipeline reads these to detect changes.

## Example Workflows

### Workflow 1: Full pipeline on Windows
```bash
python pipeline.py \
  --before-model assembly_v1.sldasm \
  --after-model assembly_v2.sldasm \
  --instructions user_instructions.pdf \
  --smg my_assembly.smg \
  --document-id MyProduct
```
Output: improved PDF with updated images

### Workflow 2: Text analysis only on Mac
```bash
# Step 1: On Windows, generate ECO
python3 << 'EOF'
from eco.synthesizer import synthesize_ecos
import json
ecos = synthesize_ecos("v1.sldasm", "v2.sldasm")
with open("eco.json", "w") as f:
    json.dump([e.model_dump() for e in ecos], f, indent=2)
EOF

# Step 2: Transfer eco.json to Mac, then run
python pipeline.py --eco eco.json --instructions instructions.pdf
```
Output: improved text with flagged images for manual update

### Workflow 3: Iterate on review
```bash
# Initial run
python pipeline.py --before-model v1.sldasm --after-model v2.sldasm --instructions guide.pdf

# Review and edit output/review_required.json

# Publish with approvals
python pipeline.py --publish <run_id_from_first_run>
```

## Questions or Issues?

Check the `.pipeline_state/` directory for intermediate results. Each stage's JSON output can help debug where something went wrong.
