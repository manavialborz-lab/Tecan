import pandas as pd
from playwright.sync_api import sync_playwright
from openai import OpenAI
import os
from datetime import datetime
import socket
from curl_cffi import requests


_original_getaddrinfo = socket.getaddrinfo

def ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

socket.getaddrinfo = ipv4_only_getaddrinfo

def fetch_page_text(url):
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
                page.wait_for_timeout(1000)
                return True
            except Exception:
                pass
        return False

    def read_with_playwright(target_url):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            try_click_cookies(page)
            text = page.inner_text("body")
            browser.close()
            if is_bad_text(text):
                return "blocked", ""
            return "ok", text

    # ============ MAIN LOGIC - KEPT SAME FORMAT ============
    
    try:
        # Publisher-specific handling
        if "wiley.com" in lower_url or "acs.org" in lower_url:
            return "metadata_only_publisher_blocked", ""

        # MDPI: Try XML endpoint first (less likely to be blocked)
        if "mdpi.com" in lower_url:
            # Try XML version first (bypasses CAPTCHA)
            xml_url = url.replace("/htm", "/xml").replace("/html", "/xml")
            if "/pdf" not in lower_url and "/xml" not in lower_url:
                try:
                    # Use curl_cffi instead of requests to avoid blocking
                    from curl_cffi import requests as curl_requests
                    response = curl_requests.get(xml_url, impersonate="chrome120", timeout=30)
                    if not is_bad_text(response.text) and len(response.text.strip()) > 200:
                        return "ok_mdpi", response.text[:100000]
                except Exception:
                    pass
            
            # Fallback to Playwright
            status, text = read_with_playwright(url)
            if status == "ok" and len(text.strip()) > 200:
                return "ok_mdpi", text
            return "metadata_only_mdpi_not_readable", ""

        # ScienceDirect
        if "sciencedirect.com" in lower_url:
            status, text = read_with_playwright(url)
            if status == "ok" and len(text.strip()) > 200:
                return "ok_sciencedirect_preview", text
            return status, text

        # All other publishers
        status, text = read_with_playwright(url)
        if status == "ok" and len(text.strip()) > 200:
            return "ok", text
        return status, text

    except Exception as exc:
        return f"playwright_error: {type(exc).__name__}: {repr(exc)}", ""


def screen_papers_with_ai(
    df,
    openai_key,
    aim,
    screening_criteria,
    stop_callback=None,
    progress_callback=None
):
    client = OpenAI(api_key=openai_key)

    flags = []
    reasons = []
    statuses = []
    evidence_sources = []
    page_text_lengths = []
    raw_answers = []

    total = len(df)

    for idx, (_, row) in enumerate(df.iterrows(), start=1):
        if stop_callback and stop_callback():
            print("Screening stopped by user.")
            break

        title = str(row.get("title", ""))
        print(f"\n[{idx}/{total}] {title[:80]}")

        if progress_callback:
            progress_callback(idx, total, title)

        status, page_text = fetch_page_text(row.get("link", ""))
        print(f"Link status: {status}")

        statuses.append(status)
        page_text_lengths.append(len(page_text))

        if status.startswith("ok") and len(page_text.strip()) > 200:
            evidence_source = "metadata_plus_page_text"
            page_text_for_llm = page_text[:5000]
        else:
            evidence_source = "metadata_only"
            page_text_for_llm = ""
            print(f"Link not readable: {status}")

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

        try:
            import socket
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You are screening papers for PMS literature review. Be strict and use only the provided evidence."
                    },
                    {
                        "role": "user",
                        "content": user_content
                    }
                ]
            )

            answer = response.choices[0].message.content.strip()

        except Exception as exc:

            answer = f"PMS_FLAG: MAYBE\nREASON: OpenAI request failed: {type(exc).__name__}: {exc}"

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

        print(f"Decision: {flag}")

        flags.append(flag)
        reasons.append(reason)

        if progress_callback:
            progress_callback(idx, total, title, flag)

    # If screening was stopped early, keep only processed rows.
    df_out = df.iloc[:len(flags)].copy()

    df_out["pms_decision"] = flags
    df_out["pms_reason"] = reasons
    df_out["link_status"] = statuses
    df_out["evidence_source"] = evidence_sources
    df_out["page_text_length"] = page_text_lengths
    df_out["openai_raw_answer"] = raw_answers
    # Save file as Excel file
    os.makedirs("Reports", exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"Reports/OpenAI_Analysis_{timestamp}.xlsx"
    
    df_out.to_excel(output_file, index=False)
    
    print(f"Saved: {output_file}")
    
    return df_out

