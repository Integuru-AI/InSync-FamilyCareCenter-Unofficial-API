"""
Download Claims by Claim IDs — InSync (Qualifacts)  [BOUNDED-PARALLEL + HARDENED]
=================================================================================
Endgame contract: def run(auth_headers, input_data) -> dict

Same behavior + output shape as the live sequential version, but:
  * per-claim work is fanned out concurrently under a semaphore (AsyncSession,
    TLS impersonation preserved for Akamai), with jittered starts;
  * upstream throttle / WAF pushback is detected and retried with backoff, then
    surfaced via an INTERNAL signal that's invisible-to-obvious for the customer
    but instantly readable by us:
      - whole-batch gate blocked  -> status_code 429 (distinct from 504 timeout /
        401 session in our integration_events telemetry)
      - per-claim issues          -> compact `_diag` tags + a batch `_diag` summary
        (counts of throttled / timeouts / session_dead). Customer-facing `error`
        strings stay generic ("Upstream temporarily unavailable, please retry.").

Tunables (input_data, optional): max_concurrency=5, request_timeout=15,
request_retries=1, start_jitter=0.25. update_attribute defaults True (destructive);
tests must pass false.
"""

ATTRIBUTE_ID_BY_NAME = {"On Hold": 68, "Charta Reviewed": 81, "VTC Grant Success": 70}
_GENERIC_UPSTREAM = "Upstream temporarily unavailable, please retry."


def run(auth_headers, input_data):
    import asyncio
    import base64
    import json
    import random
    import re
    import sys
    import time
    import urllib.parse
    from urllib.parse import urlencode

    _real = sys.modules.get("curl_cffi.requests")
    if _real is not None and hasattr(_real, "AsyncSession"):
        AsyncSession = _real.AsyncSession
    else:
        from curl_cffi.requests import AsyncSession

    base_url = (
        globals().get("BASE_URL")
        or input_data.get("base_url")
        or "https://<SUBDOMAIN>.insynchcs.com"
    ).rstrip("/")
    host = base_url.split("//", 1)[-1]

    claim_ids = input_data.get("claim_ids") or []
    if isinstance(claim_ids, (int, str)):
        claim_ids = [claim_ids]
    if not claim_ids:
        return {"status_code": 400, "body": {"error": "claim_ids is required (list of claim/charge IDs)"}}

    update_attr = input_data.get("update_attribute", True)
    download_pdf = input_data.get("download_pdf", True)
    extract_telemed = input_data.get("extract_telemed", True)
    max_concurrency = max(1, int(input_data.get("max_concurrency", 5)))
    req_timeout = float(input_data.get("request_timeout", 15))
    req_retries = max(0, int(input_data.get("request_retries", 1)))
    start_jitter = float(input_data.get("start_jitter", 0.25))

    common_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    nav_headers = {**common_headers,
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                   "Sec-Fetch-Site": "same-origin", "Upgrade-Insecure-Requests": "1"}
    xhr_headers_json = {**common_headers,
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Content-Type": "application/json; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest", "Origin": base_url,
                        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
    xhr_headers_form = {**common_headers,
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest", "Origin": base_url,
                        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}

    def _is_session_dead(resp) -> bool:
        return ("SessionTimeOut" in str(resp.url)
                or "InSync :: Session Timeout" in (resp.text or "")[:500])

    # Akamai / WAF block signatures (kept internal — NOT surfaced to the customer).
    _BLOCK_MARKERS = ("Access Denied", "Reference&#32;#", "Reference #",
                      "Pardon Our Interruption", "/errors/akamai", "ak-challenge")

    def _looks_blocked(resp) -> bool:
        try:
            if resp.status_code in (403, 429):
                return True
            head = (resp.text or "")[:800]
            return any(m in head for m in _BLOCK_MARKERS)
        except Exception:
            return False

    class _Throttled(Exception):
        """Upstream throttle/WAF block, surviving retries. Internal classifier."""

    def _b64(value) -> str:
        return base64.b64encode(str(value).encode()).decode()

    def _normalize_visit_date(visit):
        if not visit:
            return ""
        from datetime import datetime
        for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                d = datetime.strptime(visit.split("T")[0] if "T" in visit else visit, fmt)
                return f"{d.month}/{d.day}/{d.year}"
            except ValueError:
                continue
        return visit

    DT_COLS = [
        ("", "", False), ("ChargeID", "ChargeID", True),
        ("ProgramManagementDetailID", "ProgramManagementDetailID", True),
        ("CaseNumber", "CaseNumber", True), ("PatientName", "PatientName", True),
        ("DOSFrom", "DOSFrom", True), ("SubmissionDate", "SubmissionDate", True),
        ("VisitEncDate", "VisitEncDate", True), ("PayerPlanName", "PayerPlanName", True),
        ("Provider", "Provider", True), ("Category", "Category", True),
        ("Attributes", "Attributes", False), ("DSLA", "DSLA", True),
        ("Worked", "Worked", False), ("", "OnHold", False),
        ("", "ClaimSubmission", False), ("", "AgingMethod", False),
    ]

    def _search_pairs(charge_id):
        criteria = {
            "ChargeId": str(charge_id), "ClaimAttribute": None, "BalanceOperator": "Non Zero",
            "Balance": "0.00", "AgingMethod": "2", "SuperbillStatus": "0", "ArchivedClaimsFlag": "0",
            "PatientStatus": "true", "isExportToExcel": 0, "IsSetSearchCriteria": 1, "Sort": "ChargeId asc",
            "AuthorizationStatus": 0, "EncounterStatus": None, "EncounterTypeIds": None, "ChargeTypeIds": None,
            "SortbyHighestbilledamount": False, "IsShowOnlyMergedClaims": False, "IsUB04Columns": False,
            "IsClaimProcessingSearch": False, "IsVoided": False, "IsClaimMarkedAsProcessed": False,
            "IsChargesWithinFilingLimit": False, "IsTelemedicineVisit": False,
        }
        pairs = [("draw", "1")]
        for i, (data_attr, col_name, _) in enumerate(DT_COLS):
            pairs += [(f"columns[{i}][data]", data_attr), (f"columns[{i}][name]", col_name),
                      (f"columns[{i}][searchable]", "true")]
        for i, (_, _, orderable) in enumerate(DT_COLS):
            pairs.append((f"columns[{i}][orderable]", "true" if orderable else "false"))
        for i, _ in enumerate(DT_COLS):
            pairs += [(f"columns[{i}][visible]", "true"), (f"columns[{i}][search][value]", ""),
                      (f"columns[{i}][search][regex]", "false")]
        pairs += [("order[0][column]", "0"), ("order[0][dir]", "asc"), ("start", "0"),
                  ("length", "5"), ("search[value]", ""), ("search[regex]", "false"),
                  ("strClaimSearch", json.dumps(criteria)), ("strIsUserStoredPreferences", "false"),
                  ("queryId", "0")]
        return urlencode(pairs)

    async def _amain():
        async with AsyncSession(impersonate="chrome131", timeout=req_timeout) as s:
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

            async def _req(method, url, retries, **kw):
                """One request; retries transient net errors AND detected throttles
                with jittered backoff. Raises _Throttled if blocked past retries."""
                last = None
                for attempt in range(retries + 1):
                    try:
                        r = await (s.get(url, **kw) if method == "GET" else s.post(url, **kw))
                    except Exception as e:
                        last = e
                        if attempt < retries:
                            await asyncio.sleep(0.4 * (attempt + 1) + random.uniform(0, 0.4))
                            continue
                        raise
                    if _looks_blocked(r):
                        last = _Throttled()
                        if attempt < retries:
                            await asyncio.sleep(0.8 * (attempt + 1) + random.uniform(0, 0.6))
                            continue
                        raise last
                    return r
                raise last

            # ── Resilient priming gate (single point of failure for the batch) ──
            try:
                r = await _req("POST", f"{base_url}/Claims/ClaimsPageLoad", req_retries, data="",
                               headers={**xhr_headers_json, "Referer": base_url + "/Claims"},
                               allow_redirects=False)
                if _is_session_dead(r):
                    return {"status_code": 401,
                            "body": {"error": "Session expired, please re-authenticate.", "_diag": "session@priming"}}
            except _Throttled:
                # Internal-only signal: 429 + diag, generic customer text.
                return {"status_code": 429,
                        "body": {"error": _GENERIC_UPSTREAM, "_diag": "throttle@priming"}}
            except Exception as e:
                return {"status_code": 500,
                        "body": {"error": "Could not initialize claims session.", "_diag": f"priming_err:{type(e).__name__}"}}

            sem = asyncio.Semaphore(max_concurrency)

            def _stamp(out, t0):
                out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
                return out

            async def process_claim(idx, cid):
                # Stagger starts so a wave doesn't hit Akamai as one synchronized burst.
                if start_jitter:
                    await asyncio.sleep(random.uniform(0, start_jitter) * (idx % max_concurrency))
                t0 = time.monotonic()
                out = {"claim_id": cid, "errors": [], "found": False, "metadata": None,
                       "is_telemedicine": False, "telemedicine": None, "encounter_note": None,
                       "attribute_updated": False, "_diag": None}

                async with sem:
                    # (1) search grid — gating
                    try:
                        r = await _req("POST", f"{base_url}/Claims/GetSearchedCharges", req_retries,
                                       data=_search_pairs(cid),
                                       headers={**xhr_headers_form, "Referer": base_url + "/Claims/Index"},
                                       allow_redirects=False)
                        if _is_session_dead(r):
                            out["errors"].append("Session expired."); out["_diag"] = "session"
                            return _stamp(out, t0)
                        data = r.json()
                    except _Throttled:
                        out["errors"].append(_GENERIC_UPSTREAM); out["_diag"] = "throttle"
                        return _stamp(out, t0)
                    except Exception as e:
                        out["errors"].append("Claim search failed."); out["_diag"] = f"err:{type(e).__name__}"
                        return _stamp(out, t0)
                    rows = data.get("data") or []
                    row = next((d for d in rows if str(d.get("ChargeID")) == str(cid)), rows[0] if rows else None)
                    if not row:
                        out["errors"].append("not found in search results"); out["_diag"] = "notfound"
                        return _stamp(out, t0)
                    out["found"] = True

                    patient_id = row.get("PatientID"); encounter_id = row.get("EncounterID")
                    visit_date = row.get("VisitEncDate")
                    ts = re.search(r"\((\d+)\)", row.get("EncounterDate") or "")
                    encounter_ts = int(ts.group(1)) if ts else None
                    h_flag = "True" if row.get("EncounterStatus") == 3 else "False"
                    out["metadata"] = {
                        "claim_id": row.get("ChargeID"), "claim_type": row.get("ChargeType"),
                        "patient_id": patient_id, "patient_name": row.get("PatientName"),
                        "encounter_id": encounter_id, "encounter_status": row.get("EncounterStatus"),
                        "facility": row.get("FacilityName"), "provider": row.get("Provider"),
                        "payer_plan": row.get("PayerPlanName"), "visit_date": visit_date,
                        "submission_date": row.get("SubmissionDate"), "dos_from": row.get("DOSFrom"),
                        "encounter_date_ts": encounter_ts, "billing_type": row.get("BillingType"),
                        "is_generated_claim": row.get("IsGenerateClaim"),
                        "claim_submission_type": row.get("ClaimSubmissionType"),
                        "dsla": row.get("DSLA"), "dsla_detail": row.get("DSLAToolTip"),
                        "claim_status": row.get("Category"),
                    }

                    # (2) treatmentplan — gating
                    tp_url = (f"{base_url}/treatmentplan/index?page=searchclaims"
                              f"&a={_b64(patient_id)}&b={_b64(cid)}&d={_b64(encounter_id)}&h={_b64(h_flag)}")
                    try:
                        r = await _req("GET", tp_url, req_retries,
                                       headers={**nav_headers, "Referer": base_url + "/Claims/Index"},
                                       allow_redirects=True)
                        if _is_session_dead(r):
                            out["errors"].append("Session expired."); out["_diag"] = "session"
                            return _stamp(out, t0)
                    except _Throttled:
                        out["errors"].append(_GENERIC_UPSTREAM); out["_diag"] = "throttle"
                        return _stamp(out, t0)
                    except Exception as e:
                        out["errors"].append("Treatment-plan load failed."); out["_diag"] = f"err:{type(e).__name__}"
                        return _stamp(out, t0)

                    # (3) telemed — non-fatal
                    if extract_telemed:
                        try:
                            vd = _normalize_visit_date(visit_date)
                            tm_body = json.dumps({"VisitID": 0, "TeleSelectedVisitDate": vd, "ResourceId": 0})
                            r = await _req("POST", f"{base_url}/ZoomMeeting/GetTelemedicineZoomMeetingDetails",
                                           req_retries, data=tm_body, headers={**xhr_headers_json, "Referer": tp_url})
                            tm = r.json()
                            if tm.get("flag") == "1" and tm.get("data"):
                                arr = json.loads(tm["data"]) if isinstance(tm["data"], str) else tm["data"]
                                if arr:
                                    out["is_telemedicine"] = True
                                    m = arr[0]
                                    out["telemedicine"] = {
                                        "started_on": m.get("StartedOn"), "ended_on": m.get("EndedOn"),
                                        "organizer": m.get("Organizer"),
                                        "organizer_start_time": m.get("OrganizerStartTime"),
                                        "organizer_end_time": m.get("OrganizerEndTime"), "duration": m.get("Duration"),
                                        "total_participants": m.get("TotalParticipantsCount"),
                                        "participants": (m.get("Participants") or "").replace("<br>", ",").strip(", "),
                                        "cohost_details": m.get("CohostDetails"),
                                        "zoom_telemedicine_id": m.get("ZoomTelemedicineID"),
                                        "zoom_meeting_provider_url": m.get("ZoomMeetingProviderURL"),
                                        "actual_meeting_id": m.get("ActualMeetingID"),
                                        "meeting_session_id": m.get("MeetingSessionID")}
                        except _Throttled:
                            out["errors"].append(_GENERIC_UPSTREAM)
                            out["_diag"] = out["_diag"] or "throttle"
                        except Exception:
                            out["errors"].append("Telemed details unavailable.")

                    # (4) PDF — non-fatal
                    if download_pdf and encounter_id:
                        try:
                            gen_body = json.dumps({
                                "lstMultipleEncounterDetail": [{"EncounterID": encounter_id, "PatientId": patient_id,
                                    "IsMultiVisitCharge": 0, "EncounterStatus": False, "NoteStatus": 2,
                                    "isDummyEncounter": False}],
                                "patientId": 0, "isMultiVisitCharge": False, "isPrintMultiSoapNote": True})
                            r = await _req("POST", f"{base_url}/ManageCharge/GenerateSopeNote", req_retries,
                                           data=gen_body, headers={**xhr_headers_json, "Referer": base_url + "/Claims/Index"})
                            gen = r.json()
                            fp = (gen.get("FilePath") or "").strip(); err = (gen.get("ErrorMsg") or "").strip()
                            pdf_url = gen.get("tpathURL") or ""
                            if not pdf_url and fp:
                                qs = re.search(r"tpathURL=([^&]+)", fp)
                                if qs:
                                    pdf_url = urllib.parse.unquote(qs.group(1))
                            if not pdf_url:
                                if not fp and not err:
                                    out["errors"].append("encounter note unavailable (in progress or unsigned)")
                                elif err:
                                    out["errors"].append("encounter note generation error")
                                else:
                                    out["errors"].append("encounter note path missing")
                            if pdf_url:
                                pr = await _req("GET", pdf_url, req_retries,
                                                headers={**common_headers, "Accept": "application/pdf,*/*",
                                                         "Referer": base_url + "/Claims/Index"})
                                if pr.status_code == 200 and pr.content:
                                    out["encounter_note"] = {"pdf_url": pdf_url, "pdf_size": len(pr.content),
                                                             "pdf_bytes_b64": base64.b64encode(pr.content).decode()}
                                else:
                                    out["errors"].append(f"PDF GET returned {pr.status_code}")
                        except _Throttled:
                            out["errors"].append(_GENERIC_UPSTREAM)
                            out["_diag"] = out["_diag"] or "throttle"
                        except Exception:
                            out["errors"].append("encounter-note download failed")

                    # (5) destructive write — NO retry (avoid double-apply)
                    if update_attr:
                        try:
                            attr_id = ATTRIBUTE_ID_BY_NAME["Charta Reviewed"]
                            r = await s.get(f"{base_url}/Claims/UpdateChargeAttributes",
                                            params={"AttributeId": str(attr_id), "ChargeId": str(cid),
                                                    "_": str(int(time.time() * 1000))},
                                            headers={**xhr_headers_form, "Accept": "*/*",
                                                     "Referer": base_url + "/Claims/Index"})
                            resp = r.json()
                            out["attribute_updated"] = (resp.get("result") == 1)
                            if not out["attribute_updated"]:
                                out["errors"].append("attribute update did not confirm")
                        except Exception:
                            out["errors"].append("attribute update failed")

                return _stamp(out, t0)

            tasks = [process_claim(i, c) for i, c in enumerate(claim_ids)]
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            results = []
            for c, res in zip(claim_ids, gathered):
                if isinstance(res, BaseException):
                    results.append({"claim_id": c, "found": False, "metadata": None,
                                    "is_telemedicine": False, "telemedicine": None, "encounter_note": None,
                                    "attribute_updated": False, "errors": [_GENERIC_UPSTREAM],
                                    "_diag": "worker_crash", "elapsed_ms": None})
                else:
                    results.append(res)

            # Internal-only batch summary (underscore-prefixed; customer text stays generic).
            diag = {"throttled": sum(1 for r in results if r.get("_diag") == "throttle"),
                    "session_dead": sum(1 for r in results if r.get("_diag") == "session"),
                    "errors": sum(1 for r in results if r.get("_diag") and r["_diag"].startswith("err")),
                    "crashed": sum(1 for r in results if r.get("_diag") == "worker_crash"),
                    "concurrency": max_concurrency}
            return {"status_code": 200,
                    "body": {"results": results, "processed": len(results),
                             "ok": sum(1 for r in results if r["found"] and not r["errors"]),
                             "_diag": diag}}

    t_start = time.monotonic()
    result = asyncio.run(_amain())
    if isinstance(result.get("body"), dict):
        result["body"]["elapsed_ms"] = int((time.monotonic() - t_start) * 1000)
    return result


if __name__ == "__main__":
    import json as _j, sys as _sys, time as _t
    from pathlib import Path as _P
    here = _P(__file__).parent
    headers = _j.loads((here / "headers.json").read_text()) if (here / "headers.json").exists() else {}
    target_ids = [int(x) for x in _sys.argv[1:]] or [2918161, 2916671]
    t0 = _t.monotonic()
    result = run(headers, {"claim_ids": target_ids, "update_attribute": False,
                           "download_pdf": True, "extract_telemed": True, "max_concurrency": 5})
    dt = _t.monotonic() - t0
    body = result.get("body") or {}
    print(f"status={result.get('status_code')} processed={body.get('processed')} ok={body.get('ok')} "
          f"wall={dt:.1f}s _diag={body.get('_diag')}")
    for r in body.get("results", []):
        print(f"  claim {r['claim_id']}: found={r['found']} diag={r.get('_diag')} "
              f"ms={r.get('elapsed_ms')} errors={r['errors']}")
