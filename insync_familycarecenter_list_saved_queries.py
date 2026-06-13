"""
List Saved Queries — InSync (Qualifacts)
==========================================
Endgame contract: def run(auth_headers, input_data) -> dict

Returns the catalog of saved Claims-search queries the authenticated user can
see. Useful for resolving `query_name` → `query_id` ahead of `list_claim_ids`,
and for surfacing the "what queries exist" picker to a higher-level orchestrator.

Fetches the same list `list_claim_ids` consults internally during name
resolution — exposing it directly avoids needing to grep error messages for
fuzzy hints when the orchestrator wants the catalog.

Input:
    name_contains  (str, optional)  — case-insensitive substring filter on
                                       saved-query name. Example: "Limo"
                                       returns LimoTestQuery and any other
                                       containing that token.
    report_name    (str, optional)  — filter by upstream report category
                                       (default queries are all
                                       "Claims Search"; provide if you want
                                       to exclude/include other categories).

Output:
    {
        "status_code": 200,
        "body": {
            "total": <int>,
            "returned": <int>,
            "queries": [
                {
                    "query_id": <int>,
                    "query_name": <str>,
                    "report_name": <str>,
                },
                ...
            ],
        }
    }

Failure modes:
    401 — session expired (caller should re-authenticate)
    5xx — preflight failure / non-JSON response
"""


def run(auth_headers, input_data):
    import json
    import sys
    # Bypass the Lambda runtime's `_RuntimeRequestsModule` facade, which wraps
    # `from curl_cffi.requests import Session` and silently discards
    # `Session(impersonate=...)`. Without TLS impersonation, Akamai-protected
    # endpoints bounce to /SessionTimeOut even with valid cookies. Reaching
    # into `sys.modules` returns the real curl_cffi.requests module that the
    # Lambda host imported at handler load time.
    _real_curl_requests = sys.modules.get("curl_cffi.requests")
    if _real_curl_requests is not None and hasattr(_real_curl_requests, "Session"):
        Session = _real_curl_requests.Session
    else:
        from curl_cffi.requests import Session  # local-dev fallback

    base_url = (
        globals().get("BASE_URL")
        or (input_data or {}).get("base_url")
        or "https://<SUBDOMAIN>.insynchcs.com"
    ).rstrip("/")
    host = base_url.split("//", 1)[-1]

    name_filter = ((input_data or {}).get("name_contains") or "").strip().lower()
    report_filter = ((input_data or {}).get("report_name") or "").strip().lower()

    common_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": base_url,
        "Referer": base_url + "/Claims",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    def _is_session_dead(resp) -> bool:
        if "SessionTimeOut" in str(resp.url):
            return True
        # 302 redirect target may carry SessionTimeOut even when allow_redirects=False
        loc = (resp.headers or {}).get("location", "") or (resp.headers or {}).get("Location", "")
        if "SessionTimeOut" in loc:
            return True
        if "InSync :: Session Timeout" in (resp.text or "")[:500]:
            return True
        return False

    # Note: the Lambda runtime injects a _RuntimeSession proxy that does NOT
    # support the context manager protocol, so we use try/finally instead of
    # `with Session(...) as s`.
    s = Session(impersonate="chrome131", timeout=30)
    try:
        # Seed cookies
        cookie_str = (auth_headers or {}).get("Cookie", "")
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            n, v = pair.split("=", 1)
            domain = ".insynchcs.com" if n.strip() in {"ak_bmsc", "bm_sv"} else host
            try:
                s.cookies.set(n.strip(), v.strip(), domain=domain)
            except Exception:
                pass

        try:
            r = s.post(base_url + "/Claims/ClaimsPageLoad", data="",
                       headers=common_headers, allow_redirects=False)
        except Exception as e:
            return {"status_code": 500,
                    "body": {"error": f"saved-query list request failed: {e}"}}

        if _is_session_dead(r):
            return {"status_code": 401,
                    "body": {"error": "session expired (login required)"}}
        if r.status_code != 200:
            return {"status_code": r.status_code,
                    "body": {"error": f"saved-query list returned {r.status_code}"}}

        try:
            page_state = r.json()
        except Exception:
            return {"status_code": 500,
                    "body": {"error": "saved-query list returned non-JSON"}}

        catalog = page_state.get("ListOfReportQuery") or []
        out: list[dict] = []
        for q in catalog:
            qid = q.get("ParameterId")
            qname = (q.get("QueryName") or "").strip()
            rname = (q.get("ReportName") or "").strip()
            if name_filter and name_filter not in qname.lower():
                continue
            if report_filter and report_filter not in rname.lower():
                continue
            out.append({
                "query_id": int(qid) if isinstance(qid, (int, str)) and str(qid).isdigit() else qid,
                "query_name": qname,
                "report_name": rname,
            })

        # Stable sort: alphabetical by query_name (case-insensitive)
        out.sort(key=lambda q: (q["query_name"] or "").lower())

        return {
            "status_code": 200,
            "body": {
                "total": len(catalog),
                "returned": len(out),
                "queries": out,
            },
        }
    finally:
        try:
            s.close()
        except Exception:
            pass


if __name__ == "__main__":
    import json as _j
    from pathlib import Path as _P
    here = _P(__file__).parent
    headers = _j.loads((here.parent / "headers.json").read_text())

    # Demo: list all queries; then filter by "Limo".
    for inp in [{}, {"name_contains": "Limo"}]:
        r = run(headers, inp)
        body = r.get("body") or {}
        print(f"\n{'='*60}\ninput: {inp}\nstatus: {r.get('status_code')}  "
              f"total: {body.get('total')}  returned: {body.get('returned')}")
        for q in (body.get("queries") or [])[:10]:
            print(f"  {q['query_id']:>5}  {q['query_name']!r}  ({q['report_name']})")
        if body.get("returned", 0) > 10:
            print(f"  ... +{body['returned'] - 10} more")
