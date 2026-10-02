"""Command-line entry point.

Output rule: print statuses, counts and paths under $JOBAGENT_HOME only.
Never print config values, keys, resume text, or profile answers.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from jobagent.config import Settings, get_settings
from jobagent.redaction import setup_logging

log = logging.getLogger("jobagent")

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
TEX_PACKAGES = "texlive-latex-extra texlive-fonts-recommended texlive-fonts-extra lmodern poppler-utils"
HOME_SUBDIRS = ("secrets", "output", "checkpoints", "screenshots", "browser_profile")


def _display(path: Path, settings: Settings) -> str:
    """Show paths relative to $JOBAGENT_HOME so usernames don't end up in pasted output."""
    try:
        return "$JOBAGENT_HOME/" + path.relative_to(settings.jobagent_home).as_posix()
    except ValueError:
        return path.name


def cmd_init(settings: Settings, _: argparse.Namespace) -> int:
    home = settings.jobagent_home
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    for sub in HOME_SUBDIRS:
        (home / sub).mkdir(mode=0o700, exist_ok=True)
    for name in ("bullets.yaml", "resume.tex"):
        target = home / name
        if target.exists():
            print(f"kept existing {_display(target, settings)}")
        else:
            shutil.copyfile(EXAMPLES_DIR / name, target)
            target.chmod(0o600)
            print(f"created {_display(target, settings)} from examples/ (replace with your own)")
    return 0


def cmd_doctor(settings: Settings, _: argparse.Namespace) -> int:
    from jobagent import latex

    ok = True

    def report(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"[{'ok' if passed else '!!'}] {label}" + (f" ({hint})" if hint and not passed else ""))

    home = settings.jobagent_home
    report("JOBAGENT_HOME exists", home.is_dir(), "run: jobagent init")
    if home.is_dir():
        report("JOBAGENT_HOME is private (0700)", home.stat().st_mode & 0o077 == 0, "chmod 700")
    report("bullets.yaml present", settings.bullets_file.is_file())
    if settings.resume_tex.is_file():
        present = latex.markers_present(settings.resume_tex.read_text(encoding="utf-8"))
        report("resume.tex has EXPERIENCE markers", "EXPERIENCE" in present)
        print(f"     markers found: {', '.join(sorted(present)) or 'none'}")
    else:
        report("resume.tex present", False)
    for tool in ("pdflatex", "pdfinfo", "pdftotext"):
        report(f"{tool} on PATH", shutil.which(tool) is not None, f"install: {TEX_PACKAGES}")
    report("ANTHROPIC_API_KEY set", settings.anthropic_api_key is not None, "add it to .env")
    if settings.gemini_enabled:
        report("GOOGLE_API_KEY set (Gemini enabled)", settings.google_api_key is not None)
    report("LangSmith tracing off", not settings.langsmith_tracing, "on by your choice")

    import httpx

    if settings.qdrant_url:
        try:
            from jobagent.bank import BulletIndex

            BulletIndex.from_settings(settings)._client.get_collections()
            report("Qdrant reachable", True)
        except Exception as exc:  # noqa: BLE001
            report("Qdrant reachable", False, type(exc).__name__)
    else:
        report("QDRANT_URL set", False)
    if settings.ollama_base_url:
        try:
            tags = httpx.get(settings.ollama_base_url.rstrip("/") + "/api/tags", timeout=10).json()
            names = {m.get("name", "").split(":")[0] for m in tags.get("models", [])}
            report(f"Ollama has {settings.ollama_embed_model}", settings.ollama_embed_model in names,
                   f"ollama pull {settings.ollama_embed_model}")
        except Exception as exc:  # noqa: BLE001
            report("Ollama reachable", False, type(exc).__name__)
    else:
        report("OLLAMA_BASE_URL set", False)
    return 0 if ok else 1


def cmd_ingest(settings: Settings, _: argparse.Namespace) -> int:
    from jobagent.bank import BulletIndex
    from jobagent.bullets import load_bullets

    bank = load_bullets(settings.bullets_file)
    embedded, deleted = BulletIndex.from_settings(settings).sync(bank)
    total = sum(1 for _ in bank.refs())
    print(f"bullet bank synced: {total} bullets, {embedded} (re)embedded, {deleted} removed")
    return 0


def cmd_tailor(settings: Settings, args: argparse.Namespace) -> int:
    from jobagent import compile as tex_compile
    from jobagent import graph, job, tailor
    from jobagent.bank import BulletIndex
    from jobagent.bullets import load_bullets
    from jobagent.llm import coordinator_llm, writer_llm

    if args.file == "-":
        source_url, source_text = None, sys.stdin.read()
    elif args.file:
        source_url, source_text = None, Path(args.file).read_text(encoding="utf-8")
    else:
        source_url, source_text = args.url, None

    bank = load_bullets(settings.bullets_file)
    index = BulletIndex.from_settings(settings)
    coordinator, writer = coordinator_llm(settings), writer_llm(settings)
    deps = graph.Deps(
        settings=settings,
        bank=bank,
        fetch=job.fetch_job_text,
        parse=lambda text: job.parse_job(text, coordinator),
        retrieve=index.retrieve,
        write=lambda j, b, s: tailor.write(
            writer, j, b, s, settings.resume_max_bullets_per_entry, settings.resume_max_projects
        ),
        compile=lambda tex: tex_compile.compile_tex(tex, tex_inputs=settings.jobagent_home),
        pages=tex_compile.page_count,
        overflow=tex_compile.overflow_sections,
    )
    checkpointer = graph.open_checkpointer(settings)
    app = graph.build_graph(deps, checkpointer)
    config = {"configurable": {"thread_id": graph.thread_id(source_url, source_text)}}

    if not args.fresh and app.get_state(config).next:
        print("resuming the interrupted run for this posting")
        state = app.invoke(None, config)
    else:
        state = app.invoke({"source_url": source_url, "source_text": source_text}, config)

    if state.get("error"):
        print(f"resume NOT ready: {state['error']}")
        return 1
    pdf = Path(state["pdf_path"])
    print(f"resume ready: {_display(pdf, settings)} ({state['pages']} page)")
    if state.get("trims"):
        print(f"  trimmed {state['trims']} least-relevant item(s) to fit one page")
    if state.get("rephrase_rejections"):
        print(f"  {state['rephrase_rejections']} rephrasing(s) added facts and were reverted to your original text")
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # everything we create is private
    parser = argparse.ArgumentParser(prog="jobagent", description="Personal job-application agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create $JOBAGENT_HOME from examples/")
    sub.add_parser("doctor", help="check setup without printing any secret")
    sub.add_parser("ingest", help="sync bullets.yaml into the Qdrant bullet bank")
    tailor_p = sub.add_parser("tailor", help="tailor and compile the resume for one posting")
    src = tailor_p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="public job posting URL")
    src.add_argument("--file", help="file with the posting text, or - for stdin")
    tailor_p.add_argument("--fresh", action="store_true", help="ignore an unfinished run for this posting")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings)
    commands = {"init": cmd_init, "doctor": cmd_doctor, "ingest": cmd_ingest, "tailor": cmd_tailor}
    try:
        return commands[args.command](settings, args)
    except Exception as exc:  # noqa: BLE001
        # Type and redacted message only; full traceback at DEBUG.
        log.debug("command failed", exc_info=True)
        log.error("%s failed: %s: %s", args.command, type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
