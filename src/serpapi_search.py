import os
import requests
import pandas as pd
import re

url = "https://serpapi.com/search"


def is_product_number(term):
    term = str(term).strip()
    return bool(re.search(r"\d", term))


def build_queries(search_terms, context_terms):

    queries = []

    for term in search_terms:
        term = term.strip()

        if not term:
            continue

        # always search the original term
        queries.append(term)

        # if product/catalog number, combine with PM context terms
        if is_product_number(term):

            for context in context_terms:
                context = context.strip()

                if context:
                    queries.append(f'"{term}" {context}')

    return queries


def search_paper(SERPAPI_KEY, start_year, end_year, search_entities, context_terms, stop_callback=None):
    
    all_results = []
    
    queries = build_queries(search_entities, context_terms)

    for query in queries:
        if stop_callback and stop_callback():
            print("Search stopped by user.")
            break
        
        params = {
            "engine": "google_scholar",
            "q": query,
            "api_key": SERPAPI_KEY,
            "num": 20,
            "start": 0,
            "as_ylo": start_year,
            "as_yhi": end_year
         }
    
        response = requests.get(url, params=params)
        data = response.json()

        total_hits = data.get(
            "search_information", {}
        ).get("total_results", 0)

        print(f'{query} -> total hits = {total_hits}')

        # ---------- first page ----------
        results = data.get("organic_results", [])

        for r in results:
            summary = r.get(
                "publication_info", {}
            ).get("summary", "")

            all_results.append({
                "query_used": query,
                "title": r.get("title"),
                "snippet": r.get("snippet", ""),
                "summary": summary,
                "link": r.get("link")
                })

        # ---------- pagination ----------
        if total_hits > 20:

            for start in range(20, 100, 20):
                if stop_callback and stop_callback():
                    print("Search stopped by user.")
                    break

                params["start"] = start

                response = requests.get(url, params=params)
                data = response.json()

                results = data.get("organic_results", [])

                print(
                    f'{query} -> page {start}, found {len(results)}'
                )

                if len(results) == 0:
                    break

                for r in results:

                    summary = r.get(
                        "publication_info", {}
                    ).get("summary", "")

                    all_results.append({
                        "query_used": query,
                        "title": r.get("title"),
                        "snippet": r.get("snippet", ""),
                        "summary": summary,
                        "link": r.get("link")
                        })
                        
    df = pd.DataFrame(all_results)

    if df.empty:
        return df
    
    df_unique = (
        df.groupby("title", as_index=False)
          .agg({
              "query_used": lambda x: list(set(x)),
              "snippet": "first",
              "summary": "first",
              "link": "first"
          })
    )
    
    os.makedirs("Reports", exist_ok=True)
    df_unique.to_excel(f"Reports/search_result.xlsx", index=False)
    return df_unique 

 
