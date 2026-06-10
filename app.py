import streamlit as st
import pandas as pd
from io import BytesIO
import os
from dotenv import load_dotenv
from serpapi_search import search_paper
from ai_screening import screen_papers_with_ai
import subprocess
import sys

st.set_page_config(page_title="PMS Literature Screening Tool", layout="wide")

# st.markdown("""
# <style>
# .block-container {
#     padding-top: 0rem;
#     padding-left: 0rem;
#     padding-right: 0rem;
# }
# </style>
# """, unsafe_allow_html=True)

def install_playwright_browser_if_needed():
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True
        )
        print("Chromium installiert")
    except Exception as e:
        print("Playwright install failed:", e)

install_playwright_browser_if_needed()

################################################################

openai_key = os.getenv("OPENAI_API_KEY")
serpapi_key = os.getenv("SERPAPI_KEY")


###################################################################
def text_area_to_list(text):
    return [
        x.strip()
        for x in text.splitlines()
        if x.strip()
    ]


def dataframe_to_excel_bytes(df):
    buffer = BytesIO()
    df.to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer
###############################################################

def request_stop():
    st.session_state["stop_ai"] = True


def reset_stop():
    st.session_state["stop_ai"] = False


def should_stop():
    return st.session_state.get("stop_ai", False)


def request_search_stop():
    st.session_state["stop_search"] = True

def reset_search_stop():
    st.session_state["stop_search"] = False

def should_stop_search():
    return st.session_state.get("stop_search", False)
#################################################################

def render_search_tab():
    left, right = st.columns([1, 1.4])

    with left:
        st.header("Literature Search")
        st.info(
    "Search logic: If a search term contains a product or catalog number "
    "(for example a value with digits), the tool searches the term itself "
    "and also combines it with each value from 'Additional context terms'. "
    "For general text search terms without digits, only the original search term is used."
)

        # serpapi_key = st.text_input(
        #     "SerpAPI Key",
        #     type="password"
        # )

        year = st.number_input(
            "Publication year",
            min_value=2000,
            max_value=2035,
            value=2025
        )

        search_entities_text = st.text_area(
            "Search terms / product numbers",
            value="RE32453\nSaliva diagnostics steroid hormones",
            height=160
        )

        context_terms_text = st.text_area(
            "Additional context terms for product numbers",
            value="IBL International\nTecan\nrisk\ninterference\nfalse positive\nfalse negative",
            height=140
        )

        col1, col2 = st.columns(2)

        with col1:
            run_search = st.button(
                "Run Literature Search",
                on_click=reset_search_stop
        )

        with col2:
            st.button(
                "Stop Search",
                on_click=request_search_stop
        )

    with right:
        st.header("Search Report")

        if run_search:
            # if not serpapi_key:
            #     st.error("Please enter a SerpAPI key.")
            #     return

            search_entities = text_area_to_list(search_entities_text)
            context_terms = text_area_to_list(context_terms_text)

            if not search_entities:
                st.error("Please enter at least one search term or product number.")
                return

            with st.spinner("Running literature search..."):
                df_results = search_paper(
                    SERPAPI_KEY=serpapi_key,
                    Year=year,
                    search_entities=search_entities,
                    context_terms=context_terms,
                    stop_callback=should_stop_search
                )

            st.session_state["search_results"] = df_results

            st.success(f"Search finished. Found {len(df_results)} records.")
            st.dataframe(df_results, use_container_width=True)

            excel_buffer = dataframe_to_excel_bytes(df_results)

            st.download_button(
                "Download Search Results",
                data=excel_buffer,
                file_name="literature_search_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

        elif "search_results" in st.session_state:
            df_results = st.session_state["search_results"]

            st.info("Showing latest search results.")
            st.dataframe(df_results, use_container_width=True)

            excel_buffer = dataframe_to_excel_bytes(df_results)

            st.download_button(
                "Download Latest Search Results",
                data=excel_buffer,
                file_name="literature_search_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

        else:
            st.write("Search results will appear here after running the search.")


def render_ai_tab():
    left, right = st.columns([1, 1.4])
    display_cols = [
    "title",
    "pms_decision",
    "pms_reason",
    "evidence_source",
    "link_status",
    "page_text_length"]

    with left:
        st.header("AI Screening")
        st.info(
    "AI screening note: The PMS decision and reason are based on the available evidence source. "
    "If the evidence source is metadata only, the decision is based on limited information "
    "such as title, snippet, and summary. If page text was found, the decision also uses "
    "available text extracted from the paper page."
)

        # openai_key = st.text_input(
        #     "OpenAI API Key",
        #     type="password"
        # )

        uploaded_file = st.file_uploader(
            "Upload Excel or use latest search results",
            type=["xlsx"]
        )

        aim = st.text_area(
            "Aim and purpose",
            value="Identify PMS-relevant literature for diagnostic immunoassays.",
            height=120
        )

        screening_criteria = st.text_area(
            "Screening criteria",
            value="""Include papers relevant to diagnostic performance, assay limitations, interference, cross-reactivity, false positive/negative results, reliability, matrix effects, method comparison, or intended use.

Exclude animal studies, veterinary diagnostics, and papers unrelated to diagnostics or immunoassays.""",
            height=220
        )

        col1, col2 = st.columns(2)

        with col1:
            run_ai = st.button(
                "Run AI Screening",
                on_click=reset_stop
            )

        with col2:
            st.button(
                "Stop Screening",
                on_click=request_stop
            )

    with right:
        st.header("AI Screening Results")

        df_input = None

        if uploaded_file is not None:
            df_input = pd.read_excel(uploaded_file)
            st.info("Using uploaded Excel file.")

        elif "search_results" in st.session_state:
            df_input = st.session_state["search_results"]
            st.info("Using latest literature search results.")

        else:
            st.warning("Run a literature search or upload an Excel file.")

        if df_input is not None:
            st.write(f"Available papers: {len(df_input)}")
            st.dataframe(df_input.head(), use_container_width=True)

            if run_ai:
                # if not openai_key:
                #     st.error("Please enter an OpenAI API key.")
                #     return

                progress_bar = st.progress(0)
                status_box = st.empty()

                def progress_callback(current, total, title, decision=None):
                    progress_bar.progress(current / total)
                    if decision:
                        status_box.write(
                            f"Processing {current}/{total}: {title[:80]} | Decision: {decision}"
                        )
                    else:
                        status_box.write(
                            f"Processing {current}/{total}: {title[:80]}"
                        )

                with st.spinner("Running AI screening..."):
                    df_screened = screen_papers_with_ai(
                        df=df_input,
                        openai_key=openai_key,
                        aim=aim,
                        screening_criteria=screening_criteria,
                        stop_callback=should_stop,
                        progress_callback=progress_callback
                    )

                st.session_state["ai_results"] = df_screened

                if should_stop():
                    st.warning("AI screening was stopped. Partial results are shown below.")
                else:
                    st.success("AI screening finished.")

                st.dataframe(df_screened[display_cols], use_container_width=True)

                excel_buffer = dataframe_to_excel_bytes(df_screened)

                st.download_button(
                    "Download AI Screening Results",
                    data=excel_buffer,
                    file_name="AI_screened_results.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        if "ai_results" in st.session_state and not run_ai:
            df_screened = st.session_state["ai_results"]

            st.info("Showing latest AI screening results.")
            st.dataframe(st.session_state["ai_results"], use_container_width=True)
            
            excel_buffer = dataframe_to_excel_bytes(df_screened)

            st.download_button(
                "Download Latest AI Screening Results",
                data=excel_buffer,
                file_name="AI_screened_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )


def main():

    header_left, header_center, header_right = st.columns([1, 4, 1])

    with header_left:
        st.image("IBL.jpg", width=160)

    with header_center:
        st.markdown(
            """
            <h1 style="text-align:center; margin-top:10px;">
                PMS Literature Screening Tool
            </h1>
            """,
            unsafe_allow_html=True
        )

    with header_right:
        st.empty()  # keeps title centered

    st.divider()

    tab_search, tab_ai = st.tabs([
        "Literature Search",
        "AI Screening"
    ])

    with tab_search:
        render_search_tab()

    with tab_ai:
        render_ai_tab()


if __name__ == "__main__":
    main()
