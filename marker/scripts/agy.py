"""marker_agy: run Marker with an Antigravity agent as the LLM step.

  inspect  <pdf>  fast pre-flight analysis + recommended mode / LLM processor groups
  prepare  <pdf>  layout/OCR once, save checkpoint, queue LLM requests for the agent
  finalize <pdf>  replay LLM processors from checkpoint using agent answers;
                  prints new pending requests (exit 2) or renders output (exit 0)
"""
import glob
import json
import os
import pickle
import re
import sys
import time

import click

LLM_GROUP_MAP = {
    "tables": [
        "marker.processors.llm.llm_table.LLMTableProcessor",
        "marker.processors.llm.llm_table_merge.LLMTableMergeProcessor",
    ],
    "math": [
        "marker.processors.llm.llm_equation.LLMEquationProcessor",
        "marker.processors.llm.llm_mathblock.LLMMathBlockProcessor",
    ],
    "images": ["marker.processors.llm.llm_image_description.LLMImageDescriptionProcessor"],
    "forms": ["marker.processors.llm.llm_form.LLMFormProcessor"],
    "complex": ["marker.processors.llm.llm_complex.LLMComplexRegionProcessor"],
    "handwriting": ["marker.processors.llm.llm_handwriting.LLMHandwritingProcessor"],
    "headers": ["marker.processors.llm.llm_sectionheader.LLMSectionHeaderProcessor"],
    "page_correction": ["marker.processors.llm.llm_page_correction.LLMPageCorrectionProcessor"],
}
ALL_LLM = {p for paths in LLM_GROUP_MAP.values() for p in paths}
FIRST_LLM = "marker.processors.llm.llm_table.LLMTableProcessor"
SERVICE = "marker.services.antigravity.AntigravityService"

MATH_FONT_RE = re.compile(r"(cmmi|cmsy|cmex|msam|msbm|math|mtmi|mtsy|euclid|stix)", re.I)
MATH_CHAR_RE = re.compile(r"[∑∫∂√≤≥≠≈∈∉⊂⊆∪∩∇∞∀∃αβγδεθλμπρσφψωΓΔΘΛΠΣΦΨΩ]")
TABLE_LINE_RE = re.compile(r"(\S+\s{2,}\S+\s{2,}\S+|\|.+\|.+\|)")


def enabled_llm(groups_arg: str) -> set:
    groups = {g.strip().lower() for g in groups_arg.split(",") if g.strip()}
    if not groups or "none" in groups:
        return set()
    if "all" in groups:
        return set(ALL_LLM)
    bad = groups - LLM_GROUP_MAP.keys()
    if bad:
        raise click.BadParameter(f"unknown groups {sorted(bad)}; valid: {', '.join(LLM_GROUP_MAP)}, all, none")
    return {p for g in groups for p in LLM_GROUP_MAP[g]}


def split_processors(groups_arg: str):
    """Default processor list (as strings) with unselected LLM processors dropped, split at the first LLM processor."""
    from marker.converters.pdf import PdfConverter
    from marker.util import classes_to_strings

    keep = enabled_llm(groups_arg)
    paths = [p for p in classes_to_strings(list(PdfConverter.default_processors)) if p not in ALL_LLM or p in keep]
    defaults = classes_to_strings(list(PdfConverter.default_processors))
    cut = defaults.index(FIRST_LLM)
    pre_set = set(defaults[:cut])
    return [p for p in paths if p in pre_set], [p for p in paths if p not in pre_set]


def make_converter(processors, work_dir, groups_arg, artifact_dict, **opts):
    from marker.config.parser import ConfigParser
    from marker.converters.pdf import PdfConverter

    use_llm = bool(enabled_llm(groups_arg))
    parser = ConfigParser({"use_llm": use_llm, "llm_service": SERVICE, "agy_cache_dir": work_dir, **opts})
    conv = PdfConverter(
        config=parser.generate_config_dict(),
        artifact_dict=artifact_dict,
        processor_list=processors,
        renderer=parser.get_renderer(),
        llm_service=parser.get_llm_service(),
    )
    return conv, parser


def attach_loaders(document, fpath, config):
    from marker.builders.document import DocumentBuilder
    from marker.providers.registry import provider_from_filepath

    provider = provider_from_filepath(fpath)(fpath, config)
    dpi = DocumentBuilder(config).highres_image_dpi
    loader = lambda page_id: provider.get_images([page_id], dpi)[0]  # noqa: E731
    for page in document.pages:
        page._highres_loader = loader


def run_llm_round(document, conv, work_dir):
    """Run post-checkpoint processors; return requests still lacking an agent answer."""
    req_dir = os.path.join(work_dir, "requests")
    for f in glob.glob(os.path.join(req_dir, "*.json")):
        os.unlink(f)
    for proc in conv.processor_list:
        proc(document)
    pending = []
    for f in sorted(glob.glob(os.path.join(req_dir, "*.json"))):
        with open(f, encoding="utf-8") as fh:
            item = json.load(fh)
        if not os.path.exists(item["response_path"]):
            pending.append({k: item[k] for k in ("id", "schema_name", "images", "response_path")} | {"request_file": f})
    return pending


def inspect_pdf(fpath: str) -> dict:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(fpath)
    n = len(doc)
    scanned, math_pages, table_pages, images = [], [], [], 0
    for i in range(n):
        page = doc[i]
        text = page.get_textpage().get_text_bounded() or ""
        if len(text.strip()) < 50:
            scanned.append(i)
        images += sum(1 for o in page.get_objects() if o.type == 3)  # 3 = FPDF_PAGEOBJ_IMAGE
        if MATH_CHAR_RE.search(text):
            math_pages.append(i)
        if sum(1 for ln in text.splitlines() if TABLE_LINE_RE.search(ln.strip())) >= 3 or re.search(r"\btable\s+\d+", text, re.I):
            table_pages.append(i)
    doc.close()

    if not math_pages:  # symbol scan misses LaTeX-font math; check font names once via pdftext
        try:
            from pdftext.extraction import dictionary_output

            for p in dictionary_output(fpath, keep_chars=False):
                fonts = {s.get("font", {}).get("name", "") for b in p["blocks"] for ln in b["lines"] for s in ln["spans"]}
                if any(MATH_FONT_RE.search(f) for f in fonts):
                    math_pages.append(p["page"])
        except Exception:
            pass

    scanned_ratio = len(scanned) / max(1, n)
    if n <= 8 and not scanned and not table_pages:
        mode, why = "direct_agent", "Short digital PDF without tables: the agent can read it directly, no conversion needed."
    elif scanned_ratio > 0.2:
        mode, why = "balanced", f"{len(scanned)}/{n} pages look scanned: needs full OCR (--mode balanced)."
    elif not math_pages and not table_pages:
        mode, why = "fast_no_ocr", "Clean digital text, no math/tables: --mode fast --disable_ocr, no LLM step."
    else:
        mode, why = "fast", "Digital PDF with tables/math: --mode fast + agent LLM step."

    groups = [g for g, pages in (("tables", table_pages), ("math", math_pages)) if pages]
    return {
        "filepath": os.path.abspath(fpath),
        "total_pages": n,
        "scanned_pages": scanned,
        "math_pages": math_pages[:30],
        "table_candidate_pages": table_pages[:30],
        "embedded_images": images,
        "recommended_mode": mode,
        "mode_reason": why,
        "recommended_llm_processors": groups or ["none"],
        "optional_groups": [g for g in LLM_GROUP_MAP if g not in groups],
    }


def emit(obj, code=0):
    click.echo(json.dumps(obj, indent=2, ensure_ascii=False))
    sys.exit(code)


@click.group(help=__doc__)
def agy_cli():
    pass


@agy_cli.command("inspect")
@click.argument("fpath", type=click.Path(exists=True))
def inspect_cmd(fpath):
    emit(inspect_pdf(fpath))


_common = [
    click.argument("fpath", type=click.Path(exists=True)),
    click.option("--work_dir", required=True, type=click.Path()),
    click.option("--llm_processors", default="tables,math", show_default=True),
    click.option("--page_range", default=None),
]


def common(f):
    for d in reversed(_common):
        f = d(f)
    return f


@agy_cli.command("prepare")
@common
@click.option("--mode", type=click.Choice(["balanced", "fast"]), default=None)
@click.option("--disable_ocr", is_flag=True, default=False)
def prepare_cmd(fpath, work_dir, llm_processors, page_range, mode, disable_ocr):
    from marker.models import create_model_dict

    work_dir = os.path.abspath(work_dir)
    os.makedirs(work_dir, exist_ok=True)
    pre, _ = split_processors(llm_processors)
    opts = {"mode": mode, "disable_ocr": disable_ocr, "page_range": page_range, "output_format": "markdown"}

    conv, _ = make_converter(pre, work_dir, llm_processors, create_model_dict(), **opts)
    document = conv.build_document(fpath)  # layout + OCR + pre-LLM processors

    for page in document.pages:
        page.highres_image = None  # keeps the checkpoint small; re-rendered lazily
        page._highres_loader = None  # lambdas can't be pickled
    with open(os.path.join(work_dir, "checkpoint.pkl"), "wb") as f:
        pickle.dump(document, f)
    with open(os.path.join(work_dir, "settings.json"), "w") as f:
        json.dump({"llm_processors": llm_processors, **opts}, f)

    finalize(fpath, work_dir, output_dir=None)


@agy_cli.command("finalize")
@click.argument("fpath", type=click.Path(exists=True))
@click.option("--work_dir", required=True, type=click.Path(exists=True))
@click.option("--output_dir", default=None, type=click.Path(), help="Where to write output once no requests are pending.")
@click.option("--output_format", type=click.Choice(["markdown", "json", "html", "chunks"]), default="markdown")
def finalize_cmd(fpath, work_dir, output_dir, output_format):
    finalize(fpath, os.path.abspath(work_dir), output_dir, output_format)


def finalize(fpath, work_dir, output_dir, output_format="markdown"):
    from marker.output import save_output

    with open(os.path.join(work_dir, "settings.json")) as f:
        settings = json.load(f)
    groups = settings.pop("llm_processors")
    settings["output_format"] = output_format
    _, post = split_processors(groups)

    with open(os.path.join(work_dir, "checkpoint.pkl"), "rb") as f:
        document = pickle.load(f)  # fresh copy each round so replays are deterministic

    # post-checkpoint processors need no neural models
    conv, parser = make_converter(post, work_dir, groups, {}, **settings)
    attach_loaders(document, fpath, conv.config)
    pending = run_llm_round(document, conv, work_dir)

    if pending:
        emit({"status": "pending", "work_dir": work_dir, "pending_count": len(pending), "pending": pending}, code=2)
    if output_dir is None:
        emit({"status": "ready", "work_dir": work_dir, "message": "No pending requests. Run finalize with --output_dir."})

    rendered = conv.resolve_dependencies(conv.renderer)(document)
    parser.cli_options["output_dir"] = output_dir
    out = parser.get_output_folder(fpath)
    save_output(rendered, out, parser.get_base_filename(fpath))
    emit({"status": "completed", "output_folder": os.path.abspath(out)})


if __name__ == "__main__":
    agy_cli()
