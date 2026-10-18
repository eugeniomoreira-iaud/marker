---
name: marker-pdf
description: Use when the user wants to convert a PDF (paper, thesis chapter, book, scanned document) to Markdown/JSON/HTML, extract tables or equations from a PDF, or mentions Marker / marker-pdf.
---

# Marker PDF conversion (agent as the LLM step)

Marker ([datalab-to/marker](https://github.com/datalab-to/marker)) does layout/OCR locally. Its optional LLM step (table fixing, cross-page table merging, LaTeX math) normally calls the Gemini API. In this fork that step is a file queue that **you answer**. No API key is needed.

## 0. Setup (once)

```bash
command -v marker_agy || uv tool install "git+https://github.com/eugeniomoreira-iaud/marker"
```
The first `prepare` downloads the layout/OCR models (several GB). Warn the user before you run it the first time.

## 1. Inspect the PDF first (always)

```bash
marker_agy inspect "<pdf>"
```
Also open 1–2 representative pages yourself (`view_file` on the PDF) to confirm what the script reports.

## 2. Recommend, then STOP and wait for the user

Tell the user:
- what the PDF contains (pages, scanned pages, tables, math, images)
- which mode you recommend and why:

| `recommended_mode` | What to do |
|---|---|
| `direct_agent` | Skip Marker. Read the PDF directly and write the Markdown yourself. |
| `fast_no_ocr` | `marker_single "<pdf>" --mode fast --disable_ocr`, no LLM step |
| `fast` / `balanced` | Two-pass flow below, run with that `--mode` |

- which LLM groups to enable. Pre-select `recommended_llm_processors` and list `optional_groups` (`images` = AI text descriptions instead of extracted figures; `headers` = fix heading levels; `forms`, `complex`, `handwriting`, `page_correction`). Each group adds one request per matching block.

Use `ask_question` so the user can confirm or change the choices. Do not convert until they answer.

## 3. Two-pass conversion

```bash
W="<pdf_dir>/.marker_work/<pdf_stem>"
marker_agy prepare "<pdf>" --work_dir "$W" --mode fast --llm_processors tables,math
```
Output: JSON with `status`.
- `"pending"` (exit code 2): answer each item in `pending` (see 4), then run `marker_agy finalize "<pdf>" --work_dir "$W"`. Repeat until the status is no longer pending. Some answers unlock follow-up requests (e.g. merging a table chain across several pages).
- `"ready"`: run `marker_agy finalize "<pdf>" --work_dir "$W" --output_dir "<out_dir>"`. That writes `<out_dir>/<pdf_stem>/<pdf_stem>.md` plus the extracted images.

`finalize` never re-runs OCR. It replays only the LLM processors from the checkpoint.

## 4. Answering a request

Each request file (`request_file`) contains `prompt`, `images` (absolute `.webp` paths, in order), and `json_schema`.
1. `view_file` every image, then read the `prompt`. It is Marker's original Gemini prompt and it contains the draft HTML/LaTeX.
2. Write **only** a JSON object that matches `json_schema` to `response_path`.

Rules:
- Tables (`TableSchema`): put the full corrected `<table>…</table>` in `corrected_html`, or exactly `No corrections needed.`. Always set `score` to 4 or 5 and get it right in one pass. A lower score makes Marker queue another round.
- Merges (`MergeSchema`): `merge` is the string `"true"` or `"false"`, `direction` is `"bottom"` or `"right"`.
- If an image is unreadable, give the schema's "no change" answer. Never invent content.
- More than ~15 pending requests: split them across parallel subagents (give each one the request files and these rules).

## Common mistakes
- Converting before the user approved the mode and groups.
- Writing prose or a markdown fence into a response file. It must be pure JSON (invalid answers are logged and ignored).
- Leaving `--output_dir` off when status is `ready`.
- Re-running `prepare` instead of `finalize`. `prepare` redoes OCR.
