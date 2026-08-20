import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from ai_screening import screen_papers_with_ai


# Use UTF-8 for worker logs on Windows and other legacy-codepage systems.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def write_status(status_file, **updates):
    status_file = Path(status_file)

    current = {}
    if status_file.exists():
        try:
            current = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception:
            current = {}

    current.update(updates)
    current["updated_at"] = now_iso()

    temp_file = status_file.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(current, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_file.replace(status_file)


def run_job(job_dir):
    config_file = job_dir / "config.json"
    input_file = job_dir / "input.xlsx"
    status_file = job_dir / "status.json"
    stop_file = job_dir / "stop.requested"
    checkpoint_file = job_dir / "checkpoint.xlsx"
    result_file = job_dir / "result.xlsx"

    load_dotenv()

    config = json.loads(config_file.read_text(encoding="utf-8"))
    df = pd.read_excel(input_file)

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY is not available to the worker.")

    total = len(df)
    processed_at_start = 0

    if checkpoint_file.exists():
        try:
            processed_at_start = len(pd.read_excel(checkpoint_file))
        except Exception:
            processed_at_start = 0

    write_status(
        status_file,
        state="running",
        processed=processed_at_start,
        total=total,
        current_title="",
        last_decision="",
        started_at=now_iso(),
        message=(
            f"AI screening is running from paper {processed_at_start + 1}."
            if processed_at_start < total
            else "AI screening is finalizing."
        ),
    )

    def stop_callback():
        return stop_file.exists()

    def progress_callback(current, total_count, title, decision=None):
        if decision is None:
            processed = max(current - 1, processed_at_start)
            message = f"Processing paper {current}/{total_count}"
        else:
            processed = current
            message = f"Processed paper {current}/{total_count}"

        write_status(
            status_file,
            state="running",
            processed=processed,
            total=total_count,
            current_title=str(title),
            last_decision=decision or "",
            message=message,
        )

    df_out = screen_papers_with_ai(
        df=df,
        openai_key=openai_key,
        aim=config["aim"],
        screening_criteria=config["screening_criteria"],
        stop_callback=stop_callback,
        progress_callback=progress_callback,
        checkpoint_file=str(checkpoint_file),
        output_file=str(result_file),
    )

    processed = len(df_out)

    if stop_file.exists() and processed < total:
        state = "stopped"
        message = (
            f"Screening stopped after {processed} of {total} papers. "
            "The saved checkpoint can be resumed."
        )
    elif processed >= total:
        state = "completed"
        message = f"Screening completed: {processed} of {total} papers."
    else:
        state = "partial"
        message = f"Screening ended after {processed} of {total} papers."

    write_status(
        status_file,
        state=state,
        processed=processed,
        total=total,
        current_title="",
        message=message,
        finished_at=now_iso(),
        result_file=str(result_file),
    )


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python worker.py <job_directory>")

    job_dir = Path(sys.argv[1]).resolve()
    status_file = job_dir / "status.json"

    try:
        run_job(job_dir)
    except Exception as exc:
        error_text = (
            f"{type(exc).__name__}: {exc}\n\n"
            f"{traceback.format_exc()}"
        )

        try:
            (job_dir / "error.txt").write_text(error_text, encoding="utf-8")
            write_status(
                status_file,
                state="error",
                message=f"{type(exc).__name__}: {exc}",
                finished_at=now_iso(),
            )
        except Exception:
            pass

        print(error_text, file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
