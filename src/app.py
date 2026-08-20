import json
import os
import subprocess
import sys
import time
import uuid
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from serpapi_search import search_paper


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "Reports"
JOBS_DIR = REPORTS_DIR / "jobs"
CURRENT_JOB_FILE = REPORTS_DIR / "current_ai_job.txt"
WORKER_SCRIPT = Path(__file__).resolve().with_name("worker.py")
LOGO_FILE = PROJECT_ROOT / "Logo_Tecan.svg"

ACTIVE_STATES = {"starting", "queued", "running", "stopping"}
RESUMABLE_STATES = {"stopped", "partial", "error"}


st.set_page_config(
    page_title="PMS Literature Screening Tool",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1rem;
        padding-bottom: 2rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def ensure_playwright_browser():
    """Run the Playwright browser check once per Streamlit server process."""
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        print("Chromium available")
        return True
    except Exception as exc:
        print(f"Playwright browser check failed: {exc}")
        return False


ensure_playwright_browser()
load_dotenv()


def read_secret(name):
    value = os.getenv(name)
    if value:
        return value

    try:
        return st.secrets[name]
    except Exception:
        return None


openai_key = read_secret("OPENAI_API_KEY")
serpapi_key = read_secret("SERPAPI_KEY")


def text_area_to_list(text):
    return [x.strip() for x in text.splitlines() if x.strip()]


def dataframe_to_excel_bytes(df):
    buffer = BytesIO()
    df.to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer


def show_dataframe_with_numbering(df, height=None):
    df_display = df.copy()
    df_display.index = range(1, len(df_display) + 1)

    kwargs = {"width": "stretch"}
    if height is not None:
        kwargs["height"] = height

    st.dataframe(df_display, **kwargs)


def request_search_stop():
    st.session_state["stop_search"] = True


def reset_search_stop():
    st.session_state["stop_search"] = False


def should_stop_search():
    return st.session_state.get("stop_search", False)


def get_current_job_dir():
    if not CURRENT_JOB_FILE.exists():
        return None

    try:
        job_id = CURRENT_JOB_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return None

    if not job_id:
        return None

    job_dir = JOBS_DIR / job_id
    return job_dir if job_dir.exists() else None


def read_job_status(job_dir=None):
    job_dir = job_dir or get_current_job_dir()
    if job_dir is None:
        return None

    status_file = job_dir / "status.json"
    if not status_file.exists():
        return {
            "state": "starting",
            "processed": 0,
            "total": 0,
            "message": "Worker is starting.",
        }

    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except Exception:
        return {
            "state": "starting",
            "processed": 0,
            "total": 0,
            "message": "Waiting for worker status.",
        }


def write_job_status(job_dir, status):
    status_file = job_dir / "status.json"
    temp_file = status_file.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(status, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_file.replace(status_file)


def current_job_is_active():
    status = read_job_status()
    return bool(status and status.get("state") in ACTIVE_STATES)


def get_log_tail(job_dir, max_lines=8):
    log_file = job_dir / "worker.log"
    if not log_file.exists():
        return ""

    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def process_is_running(pid):
    if not pid:
        return None

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None

    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = result.stdout.lower()
            return str(pid) in output and "no tasks are running" not in output
        except Exception:
            return None

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def repair_dead_worker_status(job_dir, status):
    if not status or status.get("state") not in ACTIVE_STATES:
        return status

    running = process_is_running(status.get("pid"))
    if running is not False:
        return status

    log_tail = get_log_tail(job_dir)
    message = "The AI screening worker stopped unexpectedly."
    if log_tail:
        message += " Check the worker log for details."

    status = dict(status)
    status["state"] = "error"
    status["message"] = message
    write_job_status(job_dir, status)
    return status


def wait_for_worker_start(job_dir, process, timeout_seconds=6):
    """Catch immediate startup failures instead of leaving the UI at 'starting'."""
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        status = read_job_status(job_dir)
        if status and status.get("state") in {
            "running",
            "completed",
            "stopped",
            "partial",
            "error",
        }:
            return status

        return_code = process.poll()
        if return_code is not None:
            log_tail = get_log_tail(job_dir)
            status = status or {}
            status["state"] = "error"
            status["message"] = (
                "The AI screening worker could not start."
                + (" Check the worker log for details." if log_tail else "")
            )
            status["pid"] = process.pid
            write_job_status(job_dir, status)
            return status

        time.sleep(0.2)

    return read_job_status(job_dir)


def launch_worker(job_dir):
    env = os.environ.copy()
    if openai_key:
        env["OPENAI_API_KEY"] = str(openai_key)

    # Force UTF-8 for the background worker, especially on Windows.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    log_file = job_dir / "worker.log"
    log_handle = open(log_file, "a", encoding="utf-8")

    popen_kwargs = {
        "cwd": str(PROJECT_ROOT),
        "env": env,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }

    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            [sys.executable, str(WORKER_SCRIPT), str(job_dir)],
            **popen_kwargs,
        )
    finally:
        log_handle.close()

    status = read_job_status(job_dir) or {}
    status["pid"] = process.pid
    write_job_status(job_dir, status)

    wait_for_worker_start(job_dir, process)
    return process


def start_ai_worker(df_input, aim, screening_criteria):
    if current_job_is_active():
        raise RuntimeError(
            "An AI screening job is already running. Stop it or wait until it finishes."
        )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    input_file = job_dir / "input.xlsx"
    config_file = job_dir / "config.json"
    status_file = job_dir / "status.json"

    df_input.to_excel(input_file, index=False)
    config_file.write_text(
        json.dumps(
            {
                "aim": aim,
                "screening_criteria": screening_criteria,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    status_file.write_text(
        json.dumps(
            {
                "state": "starting",
                "processed": 0,
                "total": len(df_input),
                "message": "Starting AI screening worker.",
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    CURRENT_JOB_FILE.write_text(job_id, encoding="utf-8")
    launch_worker(job_dir)
    return job_dir


def resume_ai_worker():
    job_dir = get_current_job_dir()
    if job_dir is None:
        raise RuntimeError("There is no previous AI screening job to resume.")

    status = repair_dead_worker_status(job_dir, read_job_status(job_dir))
    state = status.get("state") if status else None

    if state in ACTIVE_STATES:
        raise RuntimeError("The current AI screening job is still running.")

    if state == "completed":
        raise RuntimeError("The current AI screening job is already completed.")

    checkpoint_file = job_dir / "checkpoint.xlsx"
    if not checkpoint_file.exists():
        raise RuntimeError("No saved checkpoint is available for this job.")

    stop_file = job_dir / "stop.requested"
    if stop_file.exists():
        stop_file.unlink()

    # A previous partial result would otherwise hide newer checkpoint rows.
    result_file = job_dir / "result.xlsx"
    if result_file.exists():
        result_file.unlink()

    status = status or {}
    status["state"] = "starting"
    status["message"] = "Resuming AI screening from the saved checkpoint."
    write_job_status(job_dir, status)

    launch_worker(job_dir)
    return job_dir


def request_worker_stop():
    job_dir = get_current_job_dir()
    if job_dir is None:
        return False

    status = repair_dead_worker_status(job_dir, read_job_status(job_dir))
    if not status or status.get("state") not in ACTIVE_STATES:
        return False

    (job_dir / "stop.requested").write_text("stop", encoding="utf-8")

    status = dict(status)
    status["state"] = "stopping"
    status["message"] = "Stop requested. The current paper will finish first."
    write_job_status(job_dir, status)
    return True


def load_current_worker_result():
    job_dir = get_current_job_dir()
    if job_dir is None:
        return None

    candidates = [
        job_dir / "result.xlsx",
        job_dir / "checkpoint.xlsx",
    ]
    existing = [path for path in candidates if path.exists()]

    if not existing:
        return None

    # Prefer the newest file so resumed checkpoints are not hidden by old results.
    file_to_read = max(existing, key=lambda path: path.stat().st_mtime)

    try:
        return pd.read_excel(file_to_read)
    except Exception:
        return None


def get_worker_ui_state():
    job_dir = get_current_job_dir()
    if job_dir is None:
        return None, None

    status = repair_dead_worker_status(job_dir, read_job_status(job_dir))
    return job_dir, status


def render_search_tab():
    left, right = st.columns([1, 1.4])

    with left:
        st.header("Literature Search")
        st.info(
            "Switch off VPN. Use one line per search term (multiple words allowed). "
            "If a catalog number is used, it is automatically connected to additional context. "
            "Search terms and context can be chosen freely."
        )

        year_col1, year_col2 = st.columns(2)

        with year_col1:
            start_year = st.number_input(
                "Start year",
                min_value=2000,
                max_value=2035,
                value=2020,
            )

        with year_col2:
            end_year = st.number_input(
                "End year",
                min_value=2000,
                max_value=2035,
                value=2025,
            )

        search_entities_text = st.text_area(
            "Search terms / product numbers",
            value="RE32453\nSaliva diagnostics steroid hormones",
            height=160,
        )

        context_terms_text = st.text_area(
            "Additional context terms for product numbers",
            value=(
                "IBL International\nTecan\nrisk\ninterference\n"
                "false positive\nfalse negative"
            ),
            height=140,
        )

        col1, col2 = st.columns(2)

        with col1:
            run_search = st.button(
                "Run Literature Search",
                on_click=reset_search_stop,
                type="primary",
                width="stretch",
            )

        with col2:
            st.button(
                "Stop Search",
                on_click=request_search_stop,
                width="stretch",
            )

    with right:
        st.header("Search Report")

        if run_search:
            search_entities = text_area_to_list(search_entities_text)
            context_terms = text_area_to_list(context_terms_text)

            if not search_entities:
                st.error("Please enter at least one search term or product number.")
                return

            if start_year > end_year:
                st.error("Start year must be smaller than or equal to end year.")
                return

            if not serpapi_key:
                st.error("SERPAPI_KEY is not available.")
                return

            try:
                with st.spinner("Running literature search..."):
                    df_results = search_paper(
                        SERPAPI_KEY=serpapi_key,
                        start_year=start_year,
                        end_year=end_year,
                        search_entities=search_entities,
                        context_terms=context_terms,
                        stop_callback=should_stop_search,
                    )
            except Exception as exc:
                st.error(f"Literature search failed: {exc}")
                return

            st.session_state["search_results"] = df_results

            if df_results.empty:
                st.info("Search finished. No records were found for these search settings.")
                return

            st.success(f"Search finished. Found {len(df_results)} records.")
            show_dataframe_with_numbering(df_results, height=520)

            st.download_button(
                "Download Search Results",
                data=dataframe_to_excel_bytes(df_results),
                file_name="literature_search_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        elif "search_results" in st.session_state:
            df_results = st.session_state["search_results"]

            if df_results.empty:
                st.info("The latest search returned no records.")
            else:
                st.info("Showing latest search results.")
                show_dataframe_with_numbering(df_results, height=520)
                st.download_button(
                    "Download Latest Search Results",
                    data=dataframe_to_excel_bytes(df_results),
                    file_name="literature_search_results.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
        else:
            st.caption("Search results will appear here after running the search.")


def render_worker_live_status_body():
    job_dir, status = get_worker_ui_state()

    if not status:
        st.caption("No active AI screening job.")
        return

    state = status.get("state", "unknown")
    processed = int(status.get("processed", 0) or 0)
    total = int(status.get("total", 0) or 0)
    remaining = max(total - processed, 0)

    state_labels = {
        "starting": "Starting",
        "queued": "Queued",
        "running": "Running",
        "stopping": "Stopping",
        "stopped": "Stopped",
        "partial": "Partial",
        "completed": "Completed",
        "error": "Error",
    }
    label = state_labels.get(state, state.title())

    if state in {"starting", "queued", "running", "stopping"}:
        status_state = "running"
    elif state == "error":
        status_state = "error"
    else:
        status_state = "complete"

    with st.container(border=True):
        if state == "error":
            st.error(f"{label} · {processed}/{total} papers")
        elif state in {"completed"}:
            st.success(f"{label} · {processed}/{total} papers")
        elif state in {"stopped", "partial"}:
            st.warning(f"{label} · {processed}/{total} papers")
        else:
            st.info(f"{label} · {processed}/{total} papers")

        if total > 0:
            st.progress(min(processed / total, 1.0))

        metric1, metric2, metric3 = st.columns(3)
        metric1.metric("Processed", processed)
        metric2.metric("Remaining", remaining)
        metric3.metric("Total", total)

        current_title = status.get("current_title")
        if current_title and state in ACTIVE_STATES:
            st.caption(f"Current paper: {current_title}")

        message = status.get("message")
        if message:
            st.write(message)

    previous_state = st.session_state.get("_last_worker_state")
    st.session_state["_last_worker_state"] = state

    if previous_state in ACTIVE_STATES and state not in ACTIVE_STATES:
        st.rerun()


def style_screening_decisions(df):
    """Highlight only the PMS decision column; do not change the data."""
    if "pms_decision" not in df.columns:
        return df

    def decision_style(value):
        decision = str(value).strip().upper()
        if decision == "YES":
            return "background-color: #fee2e2; color: #991b1b; font-weight: 700;"
        if decision == "MAYBE":
            return "background-color: #fef3c7; color: #92400e; font-weight: 700;"
        if decision == "NO":
            return "background-color: #f3f4f6; color: #374151;"
        return ""

    return df.style.map(decision_style, subset=["pms_decision"])


def show_screening_dataframe(df, height=None):
    df_display = df.copy()
    df_display.index = range(1, len(df_display) + 1)

    kwargs = {"width": "stretch"}
    if height is not None:
        kwargs["height"] = height

    st.dataframe(style_screening_decisions(df_display), **kwargs)


def render_screening_results(df_screened, display_cols):
    decisions = (
        df_screened["pms_decision"].fillna("").astype(str).str.strip().str.upper()
        if "pms_decision" in df_screened.columns
        else pd.Series("", index=df_screened.index)
    )

    yes_count = int((decisions == "YES").sum())

    # Separate high-visibility area for papers classified as YES.
    if yes_count > 0:
        with st.container(border=True):
            st.markdown(f"### 🔴 Risk-relevant papers (YES) — {yes_count} found")
            st.caption(
                "These papers were classified as risk-relevant by the AI and should be reviewed first."
            )

            risk_papers = df_screened.loc[decisions == "YES"].copy()
            preferred_risk_cols = [
                "title",
                "pms_decision",
                "pms_reason",
                "evidence_source",
                "link_status",
            ]
            risk_cols = [col for col in preferred_risk_cols if col in risk_papers.columns]
            risk_to_show = risk_papers[risk_cols] if risk_cols else risk_papers
            show_screening_dataframe(
                risk_to_show,
                height=min(340, 90 + 36 * yes_count),
            )
    else:
        with st.container(border=True):
            st.markdown("### Risk-relevant papers (YES)")
            st.success("No papers are currently flagged YES.")

    # Keep the existing complete result table underneath.
    st.subheader("Screening Results")
    st.caption("YES is highlighted in red, MAYBE in yellow, and NO in gray.")

    available_cols = [col for col in display_cols if col in df_screened.columns]
    data_to_show = df_screened[available_cols] if available_cols else df_screened
    show_screening_dataframe(data_to_show, height=440)


def render_worker_static_content(display_cols):
    job_dir, status = get_worker_ui_state()
    if not status:
        return

    state = status.get("state", "unknown")

    # Keep user-controlled expanders outside the 2-second auto-refresh fragment.
    if state == "error" and job_dir is not None:
        log_tail = get_log_tail(job_dir)
        if log_tail:
            with st.expander("Technical details", expanded=False):
                st.code(log_tail, language="text")

    df_screened = load_current_worker_result()

    if df_screened is not None and not df_screened.empty:
        render_screening_results(df_screened, display_cols)

        st.download_button(
            "Download AI Screening Results",
            data=dataframe_to_excel_bytes(df_screened),
            file_name="AI_screened_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    elif state in ACTIVE_STATES:
        st.caption(
            "Partial results become available after the first checkpoint (10 processed papers)."
        )


if hasattr(st, "fragment"):
    render_worker_live_status = st.fragment(run_every="2s")(render_worker_live_status_body)
else:
    render_worker_live_status = render_worker_live_status_body

def render_ai_tab():
    left, right = st.columns([1, 1.4])

    display_cols = [
        "title",
        "pms_decision",
        "pms_reason",
        "evidence_source",
        "link_status",
        "page_text_length",
    ]

    job_dir, current_status = get_worker_ui_state()
    current_state = current_status.get("state") if current_status else None
    is_active = current_state in ACTIVE_STATES
    checkpoint_exists = bool(job_dir and (job_dir / "checkpoint.xlsx").exists())
    can_resume = current_state in RESUMABLE_STATES and checkpoint_exists

    with left:
        st.header("AI Screening")
        st.info(
            "The search results from the literature search or an uploaded Excel file can be used. "
            "The output and decision are based on the found metadata. "
            "AI screening runs in a background worker and saves a checkpoint every 10 papers."
        )

        uploaded_file = st.file_uploader(
            "Upload Excel or use latest search results",
            type=["xlsx"],
        )

        aim = st.text_area(
            "Aim and purpose",
            value="Identify PMS-relevant literature for diagnostic immunoassays.",
            height=100,
        )

        screening_criteria = st.text_area(
            "Screening criteria",
            value="""Include papers relevant to diagnostic performance, assay limitations, interference, cross-reactivity, false positive/negative results, reliability, matrix effects, method comparison, or intended use.

Exclude animal studies, veterinary diagnostics, and papers unrelated to diagnostics or immunoassays.""",
            height=190,
        )

        df_input = None
        input_source = None

        if uploaded_file is not None:
            try:
                df_input = pd.read_excel(uploaded_file)
                input_source = "Uploaded Excel file"
            except Exception as exc:
                st.error(f"Could not read the uploaded Excel file: {exc}")
        elif "search_results" in st.session_state:
            df_input = st.session_state["search_results"]
            input_source = "Latest literature search"

        if df_input is not None:
            st.caption(f"Source: {input_source} · {len(df_input)} papers")
            with st.expander("Preview input papers"):
                show_dataframe_with_numbering(df_input.head(20), height=320)
        else:
            st.caption("No input selected yet.")

        col1, col2, col3 = st.columns(3)

        with col1:
            run_ai = st.button(
                "Run Screening",
                type="primary",
                disabled=is_active,
                width="stretch",
            )

        with col2:
            stop_ai = st.button(
                "Stop",
                disabled=not is_active,
                width="stretch",
            )

        with col3:
            resume_ai = st.button(
                "Resume",
                disabled=not can_resume,
                width="stretch",
            )

        if run_ai:
            if df_input is None or df_input.empty:
                st.error("Run a literature search or upload a non-empty Excel file first.")
            elif not openai_key:
                st.error("OPENAI_API_KEY is not available.")
            else:
                try:
                    job_dir = start_ai_worker(
                        df_input=df_input,
                        aim=aim,
                        screening_criteria=screening_criteria,
                    )
                    status = read_job_status(job_dir)
                    if status and status.get("state") == "error":
                        st.error(status.get("message", "The worker could not start."))
                    else:
                        st.session_state["show_ai_job"] = True
                        st.success("AI screening started in the background.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not start AI screening: {exc}")

        if stop_ai:
            if request_worker_stop():
                st.warning("Stop requested. The current paper will finish first.")
                st.rerun()
            else:
                st.info("There is no running AI screening job to stop.")

        if resume_ai:
            try:
                job_dir = resume_ai_worker()
                status = read_job_status(job_dir)
                if status and status.get("state") == "error":
                    st.error(status.get("message", "The worker could not resume."))
                else:
                    st.session_state["show_ai_job"] = True
                    st.success("AI screening resumed from the saved checkpoint.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not resume AI screening: {exc}")

    with right:
        st.header("AI Screening Status")

        show_job = bool(is_active or st.session_state.get("show_ai_job", False))

        if show_job:
            if hasattr(st, "fragment") and is_active:
                st.caption("Screening progress is updated automatically.")
            elif not hasattr(st, "fragment"):
                st.caption("Use the button below to update the current worker status.")
                st.button("Refresh Status")

            render_worker_live_status()
            render_worker_static_content(display_cols)
        else:
            st.caption("No active screening job. Start a new screening when you are ready.")
            if can_resume:
                st.info("A previous screening checkpoint is available. Use Resume to continue it.")


def main():
    logo_col, title_col = st.columns([0.8, 5], vertical_alignment="center")

    with logo_col:
        if LOGO_FILE.exists():
            st.image(str(LOGO_FILE), width=180)

    with title_col:
        st.markdown(
            """
            <h1 style="margin-bottom:0;">PMS Literature Screening Tool</h1>
            <p style="
                margin-top:-5px;
                color:#6b7280;
                font-size:18px;
                font-style:italic;
                font-weight:400;
            ">
                Post-Market Surveillance Literature Assessment
            </p>
            """,
            unsafe_allow_html=True,
        )

    tab_search, tab_ai = st.tabs(["Literature Search", "AI Screening"])

    with tab_search:
        render_search_tab()

    with tab_ai:
        render_ai_tab()


if __name__ == "__main__":
    main()
