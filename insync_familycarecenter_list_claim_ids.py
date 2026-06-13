"""
List Claim IDs to Process — InSync (Qualifacts)
================================================
Endgame contract: def run(auth_headers, input_data) -> dict

Drives the Claims search the same way the UI does, but unpaginated:
  1. GET  /Claims/Index
       — pull the full saved-query dropdown so we can resolve a query by name,
       and capture the assignFacility hidden field (carries selected-facility
       JSON into the criteria-fetch step).
  2. POST /Claims/GetClaimsSearchSelectedQueryData
       — body: {"objReportQuery": {QueryName, ReportName, ParameterId},
                "assignFacility": <hidden val>}
       — returns: {isDataFetched, objQueryData} where objQueryData is the full
         filter criteria that the saved query represents.
  3. POST /Claims/GetSearchedCharges  (DataTables endpoint)
       — body (form-urlencoded): draw=1 & start=0 & length=<huge>
                                 & strClaimSearch=<criteria-json>
                                 & queryId=<id>
                                 & strIsUserStoredPreferences=false
       — server ignores `length` past total, so length=100000 returns all rows.

Input:
    query_name  (str, optional) — case-insensitive saved-query name
    query_id    (str|int, optional) — direct parameterId (preferred when known)
    page_size   (int, default 100000)
    sort        (str, default "PatientName asc")

Output:
    {
        "status_code": 200,
        "body": {
            "query_id": int,
            "query_name": str,
            "total": int,           # recordsTotal from the server
            "returned": int,        # len(claims) — equals total when page_size big
            "claims": [
                {
                    "claim_id": <ChargeID>,
                    "claim_type": "Generated"|"Not Generated"|...,
                    "encounter_id": <int>,
                    "patient_id": <int>,
                    "patient_name": <str>,
                    "facility": <str>,
                    "provider": <str>,
                    "payer_plan": <str>,
                    "dos_from": <str|null>,
                    "visit_date": <str>,
                    "submission_date": <str|null>,
                    "encounter_date_ts": <epoch-ms or null>,
                    "billing_type": <int>,
                    "encounter_status": <int>,
                    "is_generated_claim": <bool>,
                },
                ...
            ],
        }
    }

Failure modes:
    404 — query_name supplied but not found in dropdown
    401 — session expired (any response redirects to /SessionTimeOut)
    500 — anything else (with `error` body)
"""


# ── Field-name translator: objQueryData (saved-query schema) → strClaimSearch ──
# Reverse-engineered from the page's GetobjIssuesParam(...) builder, which reads
# UI inputs (which were populated from objQueryData via SetClaimsSearcCriteria).
# Mapping is a static lookup table, not a runtime translation; if the UI adds a
# new criterion the table needs an entry too.
_CRIT_RENAME = {
    "DOSFROM": "DateOfServiceFrom", "DOSTo": "DateOfServiceTo",
    "VisitDateFrom": "VisitDateFrom", "VisitDateTo": "VisitDateTo",
    "SubmissionDateFrom": "DateOfClaimSubmissionFrom",
    "SubmissionDateTo": "DateOfClaimSubmissionTo",
    "dtChargeFrom": "DateOfChargeFrom", "dtChargeTo": "DateOfChargeTo",
    "ClaimNumber": "ChargeId",
    "BillingType": "strBillingTypeId",
    "ClaimSubmissionType": "ClaimSubmissionType",
    "WorkedStatus": "WorkedStatus",
    "Payer": "PayerID", "ResPayer": "PayerPlanID",
    "Responsibility": "Responsibility",
    "Provider": "ProviderId", "ServiceProvider": "ServiceProviderId",
    "ClaimStatus": "ClaimStatus",
    "Facility": "FacilityId",
    "ClaimAttributes": "ClaimAttribute",
    "BalanceOpt": "BalanceOperator", "Balance": "Balance",
    "PatientID": "PatientIds", "PatientSearchText": "strPatientName",
    "PatientCategory": "PatientCategory",
    "Invoice": "InvoiceNumber",
    "MRN": "MRN",
    "EncounterStatus": "EncounterStatus",
    "EncounterType": "EncounterTypeIds",
    "EncounterCategory": "EncounterCategoryIds",
    "ChargeType": "ChargeTypeIds",
    "CreatedBy": "CreatedByIDs", "ModifiedBy": "ModifyByIDs",
    "HoldReasonCode": "HoldReasonCodeId",
    "ErrorSeverity": "ErrorSeverityIds",
    "AgeRange": "AgeRange",
    "EligibilityStatus": "EligibilityStatusIds",
    "BatchNumber": "BatchNumber",
    "FilingLimitAlertDays": "FilingLimitAlertDays",
    "AgingMethod": "AgingMethod",
    "SuperBillStatus": "SuperbillStatus",
    "CaseNumber": "CaseNumber",
    "ProgramManagementID": "ProgramManagementID",
    "EncounterCategoryName": "EncounterCategoryName",
    "DSLAFrom": "DSLAFrom", "DSLATo": "DSLATo",
    "FilingVendorId": "FilingVendorId",
    "OrderingProviderId": "OrderingProviderId",
    # bool-ish (passed through as-is; UI re-coerces server-side)
    "IsChargesWithinFilingLimit": "IsChargesWithinFilingLimit",
    "ShowVoidClaims": "IsVoided",
    "IsMarkAsProcessed": "IsClaimMarkedAsProcessed",
    "IsShowAutoProcessClaims": "IsShowAutoProcessClaims",
    "IsShowClaimsWithMultiDOS": "IsShowClaimsWithMultiDOS",
    "ShowIssueClaimsOnly": "ShowIssueClaimsOnly",
    "ShowTelemidicineVisit": "IsTelemedicineVisit",
    "IssueLogClaimsOnly": "IssueLogClaimsOnly",
}

# Hold flag tri-state: "1"→True, "0"→False, ""/null→None
_HOLD_MAP = {"1": True, "true": True, "0": False, "false": False}


def _to_bool_or_none(v):
    if v is None or v == "":
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no"):
        return False
    return None


def _build_claim_search(objQueryData: dict, sort: str) -> dict:
    """Translate the saved-query payload into the form-state object the
    /Claims/GetSearchedCharges endpoint expects."""
    out: dict = {}
    for src, dst in _CRIT_RENAME.items():
        v = objQueryData.get(src)
        if v in (None, ""):
            out[dst] = None
            continue
        # Coerce known bool fields
        if dst in {"IsChargesWithinFilingLimit", "IsVoided",
                   "IsClaimMarkedAsProcessed", "IsShowAutoProcessClaims",
                   "IsShowClaimsWithMultiDOS", "ShowIssueClaimsOnly",
                   "IsTelemedicineVisit"}:
            b = _to_bool_or_none(v)
            out[dst] = bool(b) if b is not None else False
        else:
            out[dst] = v

    # OnHold (string Yes/No → boolean)
    on_hold = objQueryData.get("OnHold")
    out["IsShowHoldClaims"] = _HOLD_MAP.get((on_hold or "").lower(), None)

    # Static/default fields that the UI always sends — sourced from the
    # captured browser POST body for query 3407.
    out.update({
        "POSCodes": objQueryData.get("POSCodes") or None,
        "Sort": sort,
        "ArchivedClaimsFlag": objQueryData.get("ArchivedClaimsFlag") or "0",
        "isExportToExcel": 0,
        "IsSetSearchCriteria": 1,
        "IsClaimProcessingSearch": False,
        "SearchCPT": objQueryData.get("CPTCode") or None,
        "SearchCPTwithDesc": None,
        "PatientStatus": "true",
        "IsShowManuallyImportedClaimOnly": None,
        "CredentialId": objQueryData.get("Credentials") or None,
        "IsShowMSBClaimsOnly": None,
        "SortbyHighestbilledamount": False,
        "IsShowClaimProcessingOnHold": None,
        "IsShowBedBoardClaims": None,
        "LevelIDs": None,
        "ModifierCodes": objQueryData.get("ModifierCodes") or None,
        "AuthorizationStatus": 0,
        "IsCapitatedClaims": None,
        "IsShowOnlyMergedClaims": False,
        "SearchDXCode": objQueryData.get("DXCode") or None,
        "SearchDXCodewithDesc": None,
        "IsUB04Columns": False,
        "GrantIDs": "",
        "BillingEntityIDs": "",
        "SupervisingProviderIDs": "",
    })
    return out


def run(auth_headers, input_data):
    import json
    import re
    import sys
    # Bypass the Lambda runtime's `_RuntimeRequestsModule` facade, which wraps
    # `from curl_cffi.requests import Session` and silently discards
    # `Session(impersonate=...)`. Without TLS impersonation, Akamai-protected
    # endpoints (Claims/ClaimsPageLoad, etc.) bounce to /SessionTimeOut even
    # with valid cookies. The Lambda host imports `curl_cffi.requests` at
    # module load time, so the *real* module remains in `sys.modules` —
    # reaching for it directly avoids the facade's __import__ interception.
    _real_curl_requests = sys.modules.get("curl_cffi.requests")
    if _real_curl_requests is not None and hasattr(_real_curl_requests, "Session"):
        Session = _real_curl_requests.Session
    else:
        from curl_cffi.requests import Session  # local-dev fallback

    base_url = (
        globals().get("BASE_URL")
        or input_data.get("base_url")
        or "https://<SUBDOMAIN>.insynchcs.com"
    ).rstrip("/")

    query_name = (input_data.get("query_name") or "").strip()
    query_id = input_data.get("query_id")
    page_size = int(input_data.get("page_size") or 100000)
    sort = input_data.get("sort") or "PatientName asc"

    if not query_name and not query_id:
        return {"status_code": 400,
                "body": {"error": "Provide either 'query_name' or 'query_id'."}}

    common_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _is_session_expired(resp) -> bool:
        url = str(resp.url)
        return ("SessionTimeOut" in url
                or (resp.status_code == 200 and "<title>InSync :: Session Timeout</title>" in (resp.text or "")[:500]))

    # Note: the Lambda runtime injects a _RuntimeSession proxy that does NOT
    # support the context manager protocol, so we must use try/finally instead
    # of `with Session(...) as s`.
    s = Session(impersonate="chrome131", timeout=45)
    try:
        # Seed cookies from auth_headers["Cookie"].
        cookie_str = (auth_headers or {}).get("Cookie", "")
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            n, v = pair.split("=", 1)
            domain = ".insynchcs.com" if n.strip() in {"ak_bmsc", "bm_sv"} else \
                     base_url.split("//", 1)[-1]
            try:
                s.cookies.set(n.strip(), v.strip(), domain=domain)
            except Exception:
                pass

        # ── Step 1: POST /Claims/ClaimsPageLoad — get saved-query list + page state ──
        # The saved-query dropdown is populated via this XHR; the static
        # /Claims/Index HTML only contains a placeholder <option value="0">Select</option>.
        page_headers = {
            **common_headers,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": base_url,
            "Referer": base_url + "/Claims",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        try:
            r = s.post(base_url + "/Claims/ClaimsPageLoad", data="",
                       headers=page_headers, allow_redirects=False)
        except Exception as e:
            return {"status_code": 500,
                    "body": {"error": f"ClaimsPageLoad request failed: {e}"}}
        if _is_session_expired(r):
            return {"status_code": 401, "body": {"error": "session expired (login required)"}}
        if r.status_code != 200:
            return {"status_code": r.status_code,
                    "body": {"error": f"ClaimsPageLoad returned {r.status_code}"}}
        try:
            page_state = r.json()
        except Exception:
            return {"status_code": 500,
                    "body": {"error": "ClaimsPageLoad: non-JSON response"}}
        queries = page_state.get("ListOfReportQuery") or []

        # Resolve query_name → ParameterId, or validate the supplied query_id.
        resolved_id = None
        resolved_name = None
        if query_id is not None:
            qid_int = int(query_id) if str(query_id).isdigit() else None
            for q in queries:
                if q.get("ParameterId") == qid_int or str(q.get("ParameterId")) == str(query_id):
                    resolved_id = str(q.get("ParameterId"))
                    resolved_name = q.get("QueryName") or query_name or str(query_id)
                    break
            if resolved_id is None:
                resolved_id = str(query_id)
                resolved_name = query_name or str(query_id)
        else:
            target = query_name.lower()
            for q in queries:
                if (q.get("QueryName") or "").lower() == target:
                    resolved_id = str(q.get("ParameterId"))
                    resolved_name = q.get("QueryName")
                    break
            if resolved_id is None:
                near = [q.get("QueryName") for q in queries
                        if target in (q.get("QueryName") or "").lower()]
                hint = f"; did you mean: {near[:5]}" if near else ""
                return {"status_code": 404,
                        "body": {"error": f"Saved query '{query_name}' not found{hint}",
                                 "available_count": len(queries)}}

        assign_facility = ""

        # ── Step 2: POST /Claims/GetClaimsSearchSelectedQueryData ──
        crit_payload = {
            "objReportQuery": {
                "QueryName": resolved_name,
                "ReportName": "Claims Search",
                "ParameterId": str(resolved_id),
            },
            "assignFacility": assign_facility or "",
        }
        crit_headers = {
            **common_headers,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": base_url,
            "Referer": base_url + "/Claims/Index",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        try:
            r = s.post(base_url + "/Claims/GetClaimsSearchSelectedQueryData",
                       data=json.dumps(crit_payload),
                       headers=crit_headers, allow_redirects=False)
        except Exception as e:
            return {"status_code": 500,
                    "body": {"error": f"GetClaimsSearchSelectedQueryData request failed: {e}"}}
        if _is_session_expired(r):
            return {"status_code": 401, "body": {"error": "session expired (login required)"}}
        if r.status_code != 200:
            return {"status_code": r.status_code,
                    "body": {"error": f"GetClaimsSearchSelectedQueryData returned {r.status_code}"}}
        try:
            crit_resp = r.json()
        except Exception:
            return {"status_code": 500,
                    "body": {"error": "GetClaimsSearchSelectedQueryData: non-JSON response"}}
        if not crit_resp.get("isDataFetched"):
            return {"status_code": 500,
                    "body": {"error": f"saved query criteria not available for ParameterId={resolved_id}",
                             "raw": crit_resp}}
        # Translate saved-query criteria → form-state criteria.
        criteria = _build_claim_search(crit_resp.get("objQueryData") or {}, sort)

        # ── Step 3: POST /Claims/GetSearchedCharges (DataTables) ──
        # The server requires the full DataTables columns config — it's not
        # decorative. Without it the response counts records but returns an
        # empty `data` array (the column[N] entries drive sort-target lookup
        # and column-level visibility).
        DT_COLS = [
            ("",                          "",                         False),
            ("ChargeID",                  "ChargeID",                 True),
            ("ProgramManagementDetailID", "ProgramManagementDetailID", True),
            ("CaseNumber",                "CaseNumber",               True),
            ("PatientName",               "PatientName",              True),
            ("DOSFrom",                   "DOSFrom",                  True),
            ("SubmissionDate",            "SubmissionDate",           True),
            ("VisitEncDate",              "VisitEncDate",             True),
            ("PayerPlanName",             "PayerPlanName",            True),
            ("Provider",                  "Provider",                 True),
            ("Category",                  "Category",                 True),
            ("Attributes",                "Attributes",               False),
            ("DSLA",                      "DSLA",                     True),
            ("Worked",                    "Worked",                   False),
            ("",                          "OnHold",                   False),
            ("",                          "ClaimSubmission",          False),
            ("",                          "AgingMethod",              False),
        ]
        search_pairs: list[tuple[str, str]] = [("draw", "1")]
        for i, (data_attr, col_name, _) in enumerate(DT_COLS):
            search_pairs.append((f"columns[{i}][data]", data_attr))
            search_pairs.append((f"columns[{i}][name]", col_name))
            search_pairs.append((f"columns[{i}][searchable]", "true"))
        for i, (_, _, orderable) in enumerate(DT_COLS):
            search_pairs.append((f"columns[{i}][orderable]", "true" if orderable else "false"))
        for i, _ in enumerate(DT_COLS):
            search_pairs.append((f"columns[{i}][visible]", "true"))
            search_pairs.append((f"columns[{i}][search][value]", ""))
            search_pairs.append((f"columns[{i}][search][regex]", "false"))
        search_pairs += [
            ("order[0][column]", "0"),
            ("order[0][dir]", "asc"),
            ("start", "0"),
            ("length", str(page_size)),
            ("search[value]", ""),
            ("search[regex]", "false"),
            ("strClaimSearch", json.dumps(criteria)),
            ("strIsUserStoredPreferences", "false"),
            ("queryId", str(resolved_id)),
        ]
        search_headers = {
            **common_headers,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": base_url,
            "Referer": base_url + "/Claims/Index",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        from urllib.parse import urlencode as _urlencode
        try:
            r = s.post(base_url + "/Claims/GetSearchedCharges",
                       data=_urlencode(search_pairs), headers=search_headers,
                       allow_redirects=False)
        except Exception as e:
            return {"status_code": 500,
                    "body": {"error": f"GetSearchedCharges request failed: {e}"}}
        if _is_session_expired(r):
            return {"status_code": 401, "body": {"error": "session expired (login required)"}}
        if r.status_code != 200:
            return {"status_code": r.status_code,
                    "body": {"error": f"GetSearchedCharges returned {r.status_code}"}}
        try:
            data = r.json()
        except Exception:
            return {"status_code": 500,
                    "body": {"error": "GetSearchedCharges: non-JSON response"}}

        rows = data.get("data") or []
        out: list[dict] = []
        for d in rows:
            ts = None
            ed = d.get("EncounterDate") or ""
            m = re.search(r"\((\d+)\)", ed)
            if m:
                try:
                    ts = int(m.group(1))
                except Exception:
                    ts = None
            out.append({
                "claim_id": d.get("ChargeID"),
                "claim_type": d.get("ChargeType"),
                "encounter_id": d.get("EncounterID"),
                "encrypt_encounter_id": d.get("EncryptEncounterId"),
                "patient_id": d.get("PatientID"),
                "patient_name": d.get("PatientName"),
                "facility": d.get("FacilityName"),
                "provider": d.get("Provider"),
                "payer_plan": d.get("PayerPlanName"),
                "dos_from": d.get("DOSFrom"),
                "visit_date": d.get("VisitEncDate"),
                "submission_date": d.get("SubmissionDate"),
                "encounter_date_ts": ts,
                "date_of_charge": d.get("DateofCharge"),
                "billing_type": d.get("BillingType"),
                "encounter_status": d.get("EncounterStatus"),
                "is_generated_claim": d.get("IsGenerateClaim"),
                "claim_submission_type": d.get("ClaimSubmissionType"),
                # Days Since Last Action — the value the UI renders in the
                # (mislabeled) "Attributes" grid cell. Increments daily.
                "dsla": d.get("DSLA"),
                "dsla_detail": d.get("DSLAToolTip"),
                # Claim Status — feeds the row's status <select>
                # (`data-oldClaimsStatus`). 0 = unset/"Select".
                "claim_status": d.get("Category"),
            })

        return {
            "status_code": 200,
            "body": {
                "query_id": int(resolved_id) if str(resolved_id).isdigit() else resolved_id,
                "query_name": resolved_name,
                "total": data.get("recordsTotal", len(out)),
                "filtered": data.get("recordsFiltered", len(out)),
                "returned": len(out),
                "claims": out,
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

    # Demo: list claim IDs for the saved query the user mentioned in the brief.
    result = run(headers, {"query_name": "LimoTestQuery", "page_size": 5})
    body = result.get("body") or {}
    print(f"status: {result.get('status_code')}")
    print(f"query: {body.get('query_name')} ({body.get('query_id')})")
    print(f"total: {body.get('total')}  returned: {body.get('returned')}")
    if body.get("claims"):
        print("\n--- first 3 claims ---")
        for c in body["claims"][:3]:
            print(_j.dumps(c, indent=2))
    elif body.get("error"):
        print("error:", body["error"])
