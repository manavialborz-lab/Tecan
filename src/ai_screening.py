import os
import socket
import sys
import time
from datetime import datetime

import pandas as pd
from openai import OpenAI
from playwright.sync_api import sync_playwright


_original_getaddrinfo = socket.getaddrinfo


def ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = ipv4_only_getaddrinfo


def safe_print(message):
    """Print without letting a Windows console encoding crash the screening job."""
    try:
        print(message)
    except UnicodeEncodeError:
        try:
            sys.stdout.buffer.write((str(message) + "\n").encode("utf-8", errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:
            print(str(message).encode("ascii", errors="replace").decode("ascii"))


DEFAULT_CHECKPOINT_FILE = "Reports/AI_screening_checkpoint.xlsx"
CHECKPOINT_EVERY = 10
OPENAI_MAX_ATTEMPTS = 3


class BrowserReader:
    """Reuse one Chromium browser for the whole screening job."""

    def __init__(self):
        self.playwright = None
        self.browser = None

    def start(self):
        if self.browser is not None:
            return
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)

    def close(self):
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    def restart(self):
        self.close()
        self.start()

    def read(self, target_url, cookie_clicker, bad_text_checker):
        self.start()

        for attempt in range(2):
            page = None
            try:
                page = self.browser.new_page()
                page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                cookie_clicker(page)
                text = page.inner_text("body")

                if bad_text_checker(text):
                    return "blocked", ""
                return "ok", text

            except Exception:
                if attempt == 0:
                    self.restart()
                    continue
                raise
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass

        return "blocked", ""


def fetch_page_text(url, browser_reader=None):
    if not url or pd.isna(url):
        return "missing_url", ""

    url = str(url).strip()
    lower_url = url.lower()

    def is_bad_text(text):
        text_lower = text.lower()
        return (
            "access denied" in text_lower
            or "you don't have permission" in text_lower
            or "captcha" in text_lower
            or "verify you are human" in text_lower
            or "i'm not a robot" in text_lower
            or "403 forbidden" in text_lower
        )

    def try_click_cookies(page):
        cookie_selectors = [
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept')",
            "button:has-text('Accept all')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
            "button:has-text('Allow all')",
            "button:has-text('Akzeptieren')",
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Zustimmen')",
            "button:has-text('Einverstanden')",
        ]

        for selector in cookie_selectors:
            try:
                page.locator(selector).click(timeout=1500)
                page.wait_for_timeout(500)
                return True
            except Exception:
                pass
        return False

    own_reader = browser_reader is None
    reader = browser_reader or BrowserReader()

    def read_with_playwright(target_url):
        return reader.read(target_url, try_click_cookies, is_bad_text)

    try:
        if "wiley.com" in lower_url or "acs.org" in lower_url:
            return "metadata_only_publisher_blocked", ""

        if "mdpi.com" in lower_url:
            xml_url = url.replace("/htm", "/xml").replace("/html", "/xml")
            if "/pdf" not in lower_url and "/xml" not in lower_url:
                try:
                    from curl_cffi import requests as curl_requests

                    response = curl_requests.get(
                        xml_url,
                        impersonate="chrome120",
                        timeout=30,
                    )
                    if (
                        not is_bad_text(response.text)
                        and len(response.text.strip()) > 200
                    ):
                        return "ok_mdpi", response.text[:100000]
                except Exception:
                    pass

            status, text = read_with_playwright(url)
            if status == "ok" and len(text.strip()) > 200:
                return "ok_mdpi", text
            return "metadata_only_mdpi_not_readable", ""

        if "sciencedirect.com" in lower_url:
            status, text = read_with_playwright(url)
            if status == "ok" and len(text.strip()) > 200:
                return "ok_sciencedirect_preview", text
            return status, text

        status, text = read_with_playwright(url)
        if status == "ok" and len(text.strip()) > 200:
            return "ok", text
        return status, text

    except Exception as exc:
        return f"playwright_error: {type(exc).__name__}: {repr(exc)}", ""
    finally:
        if own_reader:
            reader.close()


def save_checkpoint(
    df,
    flags,
    reasons,
    statuses,
    evidence_sources,
    page_text_lengths,
    raw_answers,
    checkpoint_file=DEFAULT_CHECKPOINT_FILE,
):
    if not checkpoint_file:
        return

    checkpoint_file = str(checkpoint_file)
    checkpoint_dir = os.path.dirname(checkpoint_file)
    if checkpoint_dir:
        os.makedirs(checkpoint_dir, exist_ok=True)

    df_checkpoint = df.iloc[:len(flags)].copy()
    df_checkpoint["pms_decision"] = flags
    df_checkpoint["pms_reason"] = reasons
    df_checkpoint["link_status"] = statuses
    df_checkpoint["evidence_source"] = evidence_sources
    df_checkpoint["page_text_length"] = page_text_lengths
    df_checkpoint["openai_raw_answer"] = raw_answers

    temp_file = f"{checkpoint_file}.tmp.xlsx"
    df_checkpoint.to_excel(temp_file, index=False)
    os.replace(temp_file, checkpoint_file)

    safe_print(f"Checkpoint saved: {len(flags)} papers -> {checkpoint_file}")


def load_checkpoint_if_matching(df, checkpoint_file=DEFAULT_CHECKPOINT_FILE):
    if not checkpoint_file or not os.path.exists(checkpoint_file):
        return None

    try:
        checkpoint = pd.read_excel(checkpoint_file)

        if checkpoint.empty:
            return None

        required_columns = {
            "title",
            "pms_decision",
            "pms_reason",
            "link_status",
            "evidence_source",
            "page_text_length",
            "openai_raw_answer",
        }

        if not required_columns.issubset(checkpoint.columns):
            safe_print("Checkpoint ignored: required columns are missing.")
            return None

        if "title" not in df.columns:
            safe_print("Checkpoint ignored: current input has no title column.")
            return None

        if len(checkpoint) > len(df):
            safe_print("Checkpoint ignored: it contains more papers than the current input.")
            return None

        current_titles = (
            df.iloc[:len(checkpoint)]["title"].fillna("").astype(str).tolist()
        )
        checkpoint_titles = checkpoint["title"].fillna("").astype(str).tolist()

        if current_titles != checkpoint_titles:
            safe_print("Checkpoint ignored: it belongs to a different paper list.")
            return None

        return checkpoint

    except Exception as exc:
        safe_print(f"Could not load checkpoint: {type(exc).__name__}: {exc}")
        return None


def call_openai_with_retry(client, user_content):
    last_exc = None

    for attempt in range(1, OPENAI_MAX_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are screening papers for PMS literature review. "
                            "Be strict and use only the provided evidence."
                        ),
                    },
                    {
                        "role": "user",
                        "content": user_content,
                    },
                ],
            )
            return response.choices[0].message.content.strip()

        except Exception as exc:
            last_exc = exc
            safe_print(
                f"OpenAI attempt {attempt}/{OPENAI_MAX_ATTEMPTS} failed: "
                f"{type(exc).__name__}: {exc}"
            )
            if attempt < OPENAI_MAX_ATTEMPTS:
                time.sleep(attempt)

    return (
        "PMS_FLAG: MAYBE\n"
        f"REASON: OpenAI request failed after {OPENAI_MAX_ATTEMPTS} attempts: "
        f"{type(last_exc).__name__}: {last_exc}"
    )


def screen_papers_with_ai(
    df,
    openai_key,
    aim,
    screening_criteria,
    stop_callback=None,
    progress_callback=None,
    checkpoint_file=DEFAULT_CHECKPOINT_FILE,
    output_file=None,
):
    client = OpenAI(api_key=openai_key)

    flags = []
    reasons = []
    statuses = []
    evidence_sources = []
    page_text_lengths = []
    raw_answers = []

    total = len(df)
    start_index = 0

    checkpoint = load_checkpoint_if_matching(
        df,
        checkpoint_file=checkpoint_file,
    )

    if checkpoint is not None:
        flags = checkpoint["pms_decision"].fillna("").astype(str).tolist()
        reasons = checkpoint["pms_reason"].fillna("").astype(str).tolist()
        statuses = checkpoint["link_status"].fillna("").astype(str).tolist()
        evidence_sources = (
            checkpoint["evidence_source"].fillna("").astype(str).tolist()
        )
        page_text_lengths = checkpoint["page_text_length"].fillna(0).tolist()
        raw_answers = (
            checkpoint["openai_raw_answer"].fillna("").astype(str).tolist()
        )

        start_index = len(checkpoint)
        if start_index < total:
            safe_print(f"Checkpoint found. Resuming from paper {start_index + 1}/{total}")
        else:
            safe_print(f"Checkpoint already contains all {total} papers.")

    browser_reader = BrowserReader()

    try:
        for idx, (_, row) in enumerate(
            df.iloc[start_index:].iterrows(),
            start=start_index + 1,
        ):
            if stop_callback and stop_callback():
                save_checkpoint(
                    df,
                    flags,
                    reasons,
                    statuses,
                    evidence_sources,
                    page_text_lengths,
                    raw_answers,
                    checkpoint_file=checkpoint_file,
                )
                safe_print("Screening stopped by user.")
                break

            title = str(row.get("title", ""))
            safe_print(f"\n[{idx}/{total}] {title[:80]}")

            if progress_callback:
                progress_callback(idx, total, title)

            status, page_text = fetch_page_text(
                row.get("link", ""),
                browser_reader=browser_reader,
            )
            safe_print(f"Link status: {status}")

            statuses.append(status)
            page_text_lengths.append(len(page_text))

            if status.startswith("ok") and len(page_text.strip()) > 200:
                evidence_source = "metadata_plus_page_text"
                page_text_for_llm = page_text[:5000]
            else:
                evidence_source = "metadata_only"
                page_text_for_llm = ""
                safe_print(f"Link not readable: {status}")

            evidence_sources.append(evidence_source)

            paper_content = f"""
Title:
{row.get("title", "")}

Summary:
{row.get("summary", "")}

Snippet:
{row.get("snippet", "")}

Evidence source:
{evidence_source}

Link status:
{status}

Page Text:
{page_text_for_llm}
"""

            user_content = f"""
Aim:
{aim}

Screening criteria:
{screening_criteria}

Paper:
{paper_content}

Return EXACTLY:

PMS_FLAG: YES/MAYBE/NO
REASON: one short sentence
"""

            answer = call_openai_with_retry(client, user_content)
            raw_answers.append(answer)

            flag = ""
            reason = ""

            for line in answer.splitlines():
                if line.startswith("PMS_FLAG:"):
                    flag = line.replace("PMS_FLAG:", "").strip()
                elif line.startswith("REASON:"):
                    reason = line.replace("REASON:", "").strip()

            if not flag:
                flag = "MAYBE"
            if not reason:
                reason = "AI response could not be parsed clearly."

            safe_print(f"Decision: {flag}")

            flags.append(flag)
            reasons.append(reason)

            if len(flags) % CHECKPOINT_EVERY == 0:
                save_checkpoint(
                    df,
                    flags,
                    reasons,
                    statuses,
                    evidence_sources,
                    page_text_lengths,
                    raw_answers,
                    checkpoint_file=checkpoint_file,
                )

            if progress_callback:
                progress_callback(idx, total, title, flag)

    finally:
        browser_reader.close()

    df_out = df.iloc[:len(flags)].copy()
    df_out["pms_decision"] = flags
    df_out["pms_reason"] = reasons
    df_out["link_status"] = statuses
    df_out["evidence_source"] = evidence_sources
    df_out["page_text_length"] = page_text_lengths
    df_out["openai_raw_answer"] = raw_answers

    if output_file is None:
        os.makedirs("Reports", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"Reports/OpenAI_Analysis_{timestamp}.xlsx"
    else:
        output_file = str(output_file)
        output_dir = os.path.dirname(output_file)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

    temp_output_file = f"{output_file}.tmp.xlsx"
    df_out.to_excel(temp_output_file, index=False)
    os.replace(temp_output_file, output_file)

    save_checkpoint(
        df,
        flags,
        reasons,
        statuses,
        evidence_sources,
        page_text_lengths,
        raw_answers,
        checkpoint_file=checkpoint_file,
    )

    if len(flags) == total and checkpoint_file and os.path.exists(checkpoint_file):
        os.remove(checkpoint_file)
        safe_print("Checkpoint removed: screening completed successfully.")

    safe_print(f"Saved: {output_file}")
    return df_out
