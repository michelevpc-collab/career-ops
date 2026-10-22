"""Small, dependency-free .xlsx preview/import for the user's tracker workbook."""
from __future__ import annotations

import io
import json
import re
import sqlite3
import unicodedata
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

NS={"m":"http://schemas.openxmlformats.org/spreadsheetml/2006/main","r":"http://schemas.openxmlformats.org/officeDocument/2006/relationships","p":"http://schemas.openxmlformats.org/package/2006/relationships"}


def _col_index(reference):
    letters=re.match(r"[A-Z]+",reference or "A").group(0); n=0
    for char in letters: n=n*26+ord(char)-64
    return n-1


def _xlsx_sheets(raw: bytes):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        shared=[]
        if "xl/sharedStrings.xml" in archive.namelist():
            root=ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si",NS): shared.append("".join(x.text or "" for x in item.findall(".//m:t",NS)))
        wb=ET.fromstring(archive.read("xl/workbook.xml")); rels=ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map={r.attrib["Id"]:r.attrib["Target"] for r in rels}
        for sheet in wb.findall("m:sheets/m:sheet",NS):
            target=rel_map[sheet.attrib[f"{{{NS['r']}}}id"]]
            path=str(PurePosixPath("xl")/target) if not target.startswith("/") else target.lstrip("/")
            path=path.replace("xl/worksheets/../", "xl/")
            root=ET.fromstring(archive.read(path)); rows=[]
            for row in root.findall("m:sheetData/m:row",NS):
                values=[]
                for cell in row.findall("m:c",NS):
                    idx=_col_index(cell.attrib.get("r")); values.extend([None]*max(0,idx-len(values)))
                    typ=cell.attrib.get("t"); v=cell.find("m:v",NS); val=v.text if v is not None else None
                    if typ=="s" and val is not None:
                        try: val=shared[int(val)]
                        except (ValueError,IndexError): pass
                    elif typ=="inlineStr": val="".join(t.text or "" for t in cell.findall(".//m:t",NS))
                    elif typ=="b" and val is not None: val=val=="1"
                    values.append(val)
                rows.append((int(row.attrib.get("r",len(rows)+1)),values))
            yield sheet.attrib["name"],rows


def _cell(row,index): return (row[index] if index is not None and index<len(row) else None)
def _norm_header(value): return re.sub(r"[^a-z0-9]+"," ",str(value or "").lower()).strip()
def _norm_name(value):
    value=unicodedata.normalize("NFKD",str(value or "")).encode("ascii","ignore").decode().lower()
    value=re.sub(r"\b(ag|sa|s a|sarl|gmbh|inc|ltd|llc|plc|limited|spa|srl|suisse|switzerland)\b"," ",value)
    return " ".join(re.findall(r"[a-z0-9]+",value))

ALIASES={
 "name":("company","azienda","institution","company name","company / organization domain clue only","contact / company","organization","azienda / ente"),
 "sector":("sector / category","sector","category / sector","categoria","section","industry"),
 "categories":("other categories","other screenshot categories"),
 "hq_city":("hq city","city / area","città / area","city","location"),
 "hq_region":("hq region state","region","state"), "hq_country":("hq country","country"),
 "website":("website","official website","sito ufficiale"), "careers_url":("careers url","careers openings url","official careers page"),
 "source_url":("source evidence url","source verification","source","source url"),
 "verification_status":("verification status","research status"), "confidence":("confidence",),
 "operating_locations":("other operating locations",), "employee_count":("company size employees",), "size_band":("company size band",), "stage":("company stage",),
 "company_linkedin_url":("company linkedin url",), "contact_name":("contact name","contact person","decision maker name","recipient display names observed","contatto"),
 "contact_title":("contact title function","decision maker title","ruolo area del contatto","title","role"),
 "contact_email":("contact email","email","email address","recipient","to emails","to email s","contact / recipient email","email contact route","email / official contact route"),
 "primary_email_domain":("primary email domain","email domain"),
 "email_count_in_archive":("email count in archive",),
 "first_email_date":("first email date",), "latest_email_date":("latest email date",),
 "last_outgoing_subject":("last outgoing subject","latest sent subject"),
 "reply_evidence":("reply response evidence","relevant exact reply evidence"),
 "contact_linkedin":("contact linkedin url","linkedin profile","linkedin telefono","linkedin"),
 "outreach_status":("outreach status","stato","response status","user s classification","message classification"), "application_status":("application status","status","evidence based status"),
 "priority":("priority",), "fit_score":("fit score 1 5",), "cv_version":("cv version segment","recommended cv","suggested cv segment initial"),
 "next_action":("next action",), "follow_up_date":("follow up date","follow up previsto","follow up date"),
 "notes":("notes","note"), "source_documents":("source documents sheets",),
 "role_title":("primary target role","secondary target role","role title","position","job title"),
 "sent_at":("sent date","data invio"), "subject":("subject","last outgoing subject","latest sent subject"),
 "message_text":("body","body full","body excerpt","message body","exact incoming messages from contact","reply evidence","relevant exact reply evidence"),
 "response_status":("response status","reply response status","reply response evidence","relevant exact reply evidence","evidence based status"),
}


def _headers(rows):
    # Best header candidate among the first ten worksheet rows. Maximize recognized labels / nonempty columns.
    best=None
    known={a for vals in ALIASES.values() for a in vals}
    for pos,(excel_row,values) in enumerate(rows[:10]):
        cells=[str(v).strip() if v is not None else "" for v in values]
        norm=[_norm_header(x) for x in cells]
        recognized=sum(1 for x in norm if x in known)
        nonempty=sum(bool(x) for x in norm)
        score=recognized*4+nonempty
        if nonempty>=2 and (best is None or score>best[0]): best=(score,pos,excel_row,cells)
    return best


def _mapping(headers):
    by_norm={_norm_header(h):i for i,h in enumerate(headers) if h}
    result={}
    for field,aliases in ALIASES.items():
        for alias in aliases:
            if _norm_header(alias) in by_norm: result[field]=by_norm[_norm_header(alias)]; break
    return result


def inspect_workbook(raw: bytes, db_path=None):
    sheets=[]; normalized_company_names=[]; unique_source_names={}; inferred_company_rows=0
    for name,rows in _xlsx_sheets(raw):
        candidate=_headers(rows)
        if not candidate:
            sheets.append({"name":name,"row_count":len(rows),"header_row":None,"columns":[],"data_rows":0,"duplicates":0,"company_rows":0,"contact_rows":0,"outreach_rows":0})
            continue
        _,pos,header_row,headers=candidate; mapping=_mapping(headers); data=rows[pos+1:]
        usable=[r for _,r in data if any(x not in (None,"") for x in r)]
        kind_counts={"company_rows":0,"contact_rows":0,"outreach_rows":0}
        for _,r in data:
            if not any(x not in (None,"") for x in r): continue
            if mapping.get("name") is not None and _cell(r,mapping["name"]):
                candidate=str(_cell(r,mapping["name"])).strip()
                if not re.fullmatch(r"\d+(?:\.0)?",candidate):
                    kind_counts["company_rows"]+=1
                    normalized=_norm_name(candidate); normalized_company_names.append(normalized)
                    if "clue only" in _norm_header(headers[mapping["name"]]): inferred_company_rows+=1
                    elif normalized: unique_source_names.setdefault(normalized,candidate)
            if any(_cell(r,mapping.get(k)) for k in ("contact_email","contact_name") if mapping.get(k) is not None): kind_counts["contact_rows"]+=1
            if any(_cell(r,mapping.get(k)) for k in ("subject","message_text","outreach_status","application_status") if mapping.get(k) is not None): kind_counts["outreach_rows"]+=1
        sheets.append({"name":name,"row_count":len(rows),"header_row":header_row,"columns":[h for h in headers if h],"detected_fields":list(mapping),"data_rows":len(usable),"duplicates":0,**kind_counts})
    from collections import Counter
    counts=Counter(n for n in normalized_company_names if n)
    result={"sheets":sheets,"duplicate_name_rows":sum(v-1 for v in counts.values() if v>1),"candidate_company_rows":sum(x["company_rows"] for x in sheets),"inferred_company_rows":inferred_company_rows,"unique_source_company_names":len({n for n in counts if n}),"candidate_contact_rows":sum(x["contact_rows"] for x in sheets),"candidate_outreach_rows":sum(x["outreach_rows"] for x in sheets)}
    if db_path:
        matched=new=ambiguous=0; review=[]
        with closing(sqlite3.connect(str(db_path))) as db:
            for name in unique_source_names.values():
                company_id,candidates=_match_company(db,name)
                if company_id: matched+=1
                elif candidates:
                    ambiguous+=1
                    if len(review)<30: review.append({"kind":"ambiguous","name":name,"candidates":[candidate["name"] for candidate in candidates]})
                else:
                    new+=1
                    if len(review)<30: review.append({"kind":"new_company","name":name,"candidates":[]})
        result.update(existing_company_matches=matched,new_company_proposals=new,ambiguous_company_matches=ambiguous,company_match_preview=review)
    return result


def _clean(value):
    if value is None: return None
    value=str(value).strip()
    return value if value else None


def _confidence(value):
    if value is None: return 0.55
    try: return max(0.0,min(1.0,float(str(value).replace("%", ""))/(100 if "%" in str(value) else 1)))
    except (TypeError,ValueError):
        text=str(value).strip().lower()
        return {"high":.8,"medium":.6,"moderate":.6,"low":.35,"verified":.9}.get(text,.55)


def _match_company(db, name):
    key=_norm_name(name)
    rows=db.execute("SELECT id,name,normalized_name FROM companies").fetchall()
    exact=[r for r in rows if _norm_name(r[1])==key or _norm_name(r[2])==key]
    if len(exact)==1: return exact[0][0],[]
    tokens=set(key.split()); candidates=[]
    if tokens:
        for row in rows:
            other=set(_norm_name(row[1]).split()); overlap=len(tokens&other)/max(len(tokens|other),1)
            if overlap>=.72: candidates.append((overlap,row[0],row[1]))
    candidates=sorted(candidates,reverse=True)[:5]
    if len(candidates)==1 and candidates[0][0]>=.9: return candidates[0][1],[]
    return None,[{"company_id":c[1],"name":c[2],"score":round(c[0],2)} for c in candidates]


def import_workbook(db_path, raw: bytes, filename: str, backup_path=None):
    """Transactional, additive import. Existing populated company values are retained."""
    import hashlib
    fingerprint=hashlib.sha256(raw).hexdigest()
    with closing(sqlite3.connect(str(db_path))) as prior_db:
        prior=prior_db.execute("SELECT report_json FROM workbook_imports WHERE file_fingerprint=?",(fingerprint,)).fetchone()
    if prior:
        previous=json.loads(prior[0]); previous["already_imported"]=True
        return previous
    if backup_path:
        source=sqlite3.connect(str(db_path)); target=sqlite3.connect(str(backup_path)); source.backup(target); target.close(); source.close()
    preview=inspect_workbook(raw,db_path); now=datetime.now(timezone.utc).isoformat(timespec="seconds")
    report={"filename":filename,"companies_inserted":0,"companies_updated":0,"contacts_imported":0,"outreach_records_imported":0,"applications_imported":0,"unmatched_rows":0,"ambiguous_rows":0,"duplicates_skipped":0,"sheets":preview["sheets"]}
    with closing(sqlite3.connect(str(db_path))) as db:
        db.execute("PRAGMA foreign_keys=ON"); db.execute("BEGIN IMMEDIATE")
        cur=db.execute("INSERT INTO workbook_imports(source_filename,imported_at,report_json,file_fingerprint) VALUES(?,?, '{}',?)",(filename,now,fingerprint)); import_id=cur.lastrowid
        processed_fingerprints=set()
        inserted_company_ids=set(); updated_company_ids=set()
        for sheet,rows in _xlsx_sheets(raw):
            h=_headers(rows)
            if not h: continue
            _,pos,header_row,headers=h; mapping=_mapping(headers)
            for excel_row,values in rows[pos+1:]:
                if not any(v not in (None,"") for v in values): continue
                name=_clean(_cell(values,mapping.get("name"))) if mapping.get("name") is not None else None
                if name and re.fullmatch(r"\d+(?:\.0)?",name): name=None
                row_object={str(headers[i] if i<len(headers) and headers[i] else f"column_{i+1}"):v for i,v in enumerate(values) if v not in (None,"")}
                fields={field:_clean(_cell(values,index)) for field,index in mapping.items() if field not in {"name","contact_name","contact_title","contact_email","contact_linkedin","role_title","sent_at","subject","message_text","response_status"}}
                company_id=None; matches=[]
                inferred_header=_norm_header(headers[mapping["name"]]) if mapping.get("name") is not None else ""
                inferred_company="clue only" in inferred_header or inferred_header=="contact company"
                is_new_company=False
                if name:
                    company_id,matches=_match_company(db,name)
                    if company_id is None and not matches:
                        nkey=_norm_name(name)
                        if not nkey or inferred_company: company_id=None
                        else:
                            cur=db.execute("INSERT INTO companies(name,normalized_name) VALUES(?,?)",(name,nkey)); company_id=cur.lastrowid; inserted_company_ids.add(company_id); is_new_company=True
                    if company_id:
                        changed=False
                        existing_fields={field:db.execute(f"SELECT {field} FROM companies WHERE id=?",(company_id,)).fetchone()[0] for field in fields}
                        for field,val in fields.items():
                            if val and not existing_fields.get(field):
                                db.execute(f"UPDATE companies SET {field}=? WHERE id=?",(val,company_id)); changed=True
                            if val:
                                source_value=fields.get("source_url")
                                evidence_url=source_value if source_value and source_value.lower().startswith(("http://","https://")) else f"workbook://{filename}#{sheet}:{excel_row}"
                                confidence=_confidence(fields.get("confidence"))
                                verified=str(fields.get("verification_status") or "").strip().lower() in {"verified","manually verified","user verified"}
                                exists=db.execute("SELECT 1 FROM research_evidence WHERE company_id=? AND field_name=? AND field_value=? AND source_url=? AND source_kind='source_workbook' LIMIT 1",(company_id,field,val,evidence_url)).fetchone()
                                if not exists:
                                    db.execute("INSERT INTO research_evidence(company_id,field_name,field_value,source_url,retrieved_at,confidence,verification_status,source_kind,excerpt) VALUES(?,?,?,?,?,?,?,?,?)",(company_id,field,val,evidence_url,now,confidence,"verified" if verified else "unverified","source_workbook",f"{sheet} row {excel_row}"))
                                if field in {"sector","hq_city","hq_country","website","careers_url","company_linkedin_url","employee_count","stage","hq_region","operating_locations","size_band"}:
                                    state="verified" if verified else "unverified"
                                    db.execute("INSERT INTO research_field_state(company_id,field_name,state,updated_at,detail) VALUES(?,?,?,?,?) ON CONFLICT(company_id,field_name) DO UPDATE SET state=CASE WHEN research_field_state.state='verified' THEN 'verified' ELSE excluded.state END,updated_at=excluded.updated_at,detail=excluded.detail",(company_id,field,state,now,f"Imported from {sheet} row {excel_row}."))
                        if changed and not is_new_company: updated_company_ids.add(company_id)
                    if company_id:
                        source_url=fields.get("source_url")
                        db.execute("INSERT INTO company_sources(company_id,worksheet,worksheet_row,source_name,source_url,source_data_json) VALUES(?,?,?,?,?,?) ON CONFLICT(worksheet,worksheet_row) DO UPDATE SET source_url=COALESCE(company_sources.source_url,excluded.source_url),source_data_json=CASE WHEN company_sources.source_data_json='{}' THEN excluded.source_data_json ELSE company_sources.source_data_json END",(company_id,sheet,excel_row,name,source_url,json.dumps(row_object,ensure_ascii=False,default=str)))
                if not company_id and mapping.get("contact_email") is not None:
                    raw_email=_clean(_cell(values,mapping["contact_email"]))
                    email_match=re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",raw_email or "",re.I)
                    if email_match:
                        matches_by_email=db.execute("SELECT DISTINCT company_id FROM contacts WHERE lower(email)=lower(?) AND company_id IS NOT NULL",(email_match.group(0),)).fetchall()
                        if len(matches_by_email)==1: company_id=matches_by_email[0][0]
                        elif len(matches_by_email)>1:
                            matches=[{"company_id":r[0],"name":db.execute("SELECT name FROM companies WHERE id=?",(r[0],)).fetchone()[0],"score":1.0} for r in matches_by_email]
                has_contact=any(mapping.get(k) is not None and _clean(_cell(values,mapping[k])) for k in ("contact_email","contact_name"))
                has_outreach=any(mapping.get(k) is not None and _clean(_cell(values,mapping[k])) for k in ("subject","message_text","outreach_status","application_status"))
                if name and company_id is None:
                    db.execute("INSERT INTO unmatched_import_rows(import_id,sheet_name,source_row,record_kind,proposed_name,match_candidates,row_json) VALUES(?,?,?,?,?,?,?)",(import_id,sheet,excel_row,"ambiguous_company",name,json.dumps(matches),json.dumps(row_object,ensure_ascii=False,default=str)))
                    report["unmatched_rows"]+=1; report["ambiguous_rows"]+=1
                if has_contact and company_id:
                    email_raw=_clean(_cell(values,mapping.get("contact_email"))) if mapping.get("contact_email") is not None else None
                    email=(re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",email_raw,re.I).group(0).lower() if email_raw and re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",email_raw,re.I) else None)
                    contact_name=_clean(_cell(values,mapping.get("contact_name"))) if mapping.get("contact_name") is not None else None
                    title=_clean(_cell(values,mapping.get("contact_title"))) if mapping.get("contact_title") is not None else None
                    linkedin_raw=_clean(_cell(values,mapping.get("contact_linkedin"))) if mapping.get("contact_linkedin") is not None else None
                    linkedin=linkedin_raw if linkedin_raw and linkedin_raw.lower().startswith("http") else None
                    if email or contact_name:
                        existing_name=(db.execute("SELECT 1 FROM contacts WHERE company_id=? AND name IS ? AND title IS ? LIMIT 1",(company_id,contact_name,title)).fetchone() if not email else None)
                        if existing_name: continue
                        db.execute("INSERT INTO contacts(company_id,name,title,email,linkedin_url,source_url,source_sheet,confidence,verification_status,source_data_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_id,email) DO UPDATE SET name=COALESCE(contacts.name,excluded.name),title=COALESCE(contacts.title,excluded.title),linkedin_url=COALESCE(contacts.linkedin_url,excluded.linkedin_url),source_url=COALESCE(contacts.source_url,excluded.source_url),source_data_json=excluded.source_data_json,updated_at=excluded.updated_at",(company_id,contact_name,title,email,linkedin,fields.get("source_url"),sheet,.7,"source_workbook",json.dumps(row_object,ensure_ascii=False,default=str),now,now))
                        report["contacts_imported"]+=1
                subject=_clean(_cell(values,mapping.get("subject"))) if mapping.get("subject") is not None else None
                message=_clean(_cell(values,mapping.get("message_text"))) if mapping.get("message_text") is not None else None
                classification=_clean(_cell(values,mapping.get("outreach_status"))) if mapping.get("outreach_status") is not None else None
                response=_clean(_cell(values,mapping.get("response_status"))) if mapping.get("response_status") is not None else None
                action=_clean(_cell(values,mapping.get("next_action"))) if mapping.get("next_action") is not None else None
                if has_outreach and (subject or message or response or classification or action):
                    # Fingerprint uses row content only inside the local DB; no message content is logged.
                    import hashlib
                    fingerprint=hashlib.sha256((sheet+"\0"+str(excel_row)+"\0"+json.dumps(row_object,ensure_ascii=False,default=str,sort_keys=True)).encode()).hexdigest()
                    if fingerprint in processed_fingerprints: report["duplicates_skipped"]+=1; continue
                    processed_fingerprints.add(fingerprint)
                    raw_contact_email=_clean(_cell(values,mapping.get("contact_email"))) if mapping.get("contact_email") is not None else None
                    email_match=re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",raw_contact_email or "",re.I)
                    contact_email=email_match.group(0).lower() if email_match else None
                    sent_at=_clean(_cell(values,mapping.get("sent_at"))) if mapping.get("sent_at") is not None else None
                    existed=db.execute("SELECT 1 FROM outreach_history WHERE source_fingerprint=?",(fingerprint,)).fetchone()
                    db.execute("INSERT INTO outreach_history(company_id,contact_email,subject,message_text,sent_at,response_status,classification,next_action,source_sheet,source_row,source_fingerprint,created_at,source_data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_fingerprint) DO UPDATE SET contact_email=COALESCE(outreach_history.contact_email,excluded.contact_email),subject=COALESCE(outreach_history.subject,excluded.subject),message_text=COALESCE(outreach_history.message_text,excluded.message_text),response_status=COALESCE(outreach_history.response_status,excluded.response_status),classification=COALESCE(outreach_history.classification,excluded.classification),next_action=COALESCE(outreach_history.next_action,excluded.next_action),source_data_json=CASE WHEN outreach_history.source_data_json='{}' THEN excluded.source_data_json ELSE outreach_history.source_data_json END",(company_id,contact_email,subject,message,sent_at,response,classification,action,sheet,excel_row,fingerprint,now,json.dumps(row_object,ensure_ascii=False,default=str)))
                    if not existed: report["outreach_records_imported"]+=1
                    else: report["duplicates_skipped"]+=1
                role=_clean(_cell(values,mapping.get("role_title"))) if mapping.get("role_title") is not None else None
                app_status=_clean(_cell(values,mapping.get("application_status"))) if mapping.get("application_status") is not None else None
                if company_id and (role or app_status):
                    import hashlib
                    app_fingerprint=hashlib.sha256((sheet+"\0"+str(excel_row)+"\0"+json.dumps(row_object,ensure_ascii=False,default=str,sort_keys=True)).encode()).hexdigest()
                    existed=db.execute("SELECT 1 FROM application_records WHERE source_fingerprint=?",(app_fingerprint,)).fetchone()
                    db.execute("INSERT INTO application_records(company_id,role_title,status,cv_version,next_action,follow_up_at,source_url,source_sheet,created_at,updated_at,source_fingerprint,source_data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_fingerprint) DO UPDATE SET role_title=COALESCE(application_records.role_title,excluded.role_title),status=COALESCE(application_records.status,excluded.status),cv_version=COALESCE(application_records.cv_version,excluded.cv_version),next_action=COALESCE(application_records.next_action,excluded.next_action),source_data_json=CASE WHEN application_records.source_data_json='{}' THEN excluded.source_data_json ELSE application_records.source_data_json END",(company_id,role,app_status,fields.get("cv_version"),fields.get("next_action"),fields.get("follow_up_date"),fields.get("source_url"),sheet,now,now,app_fingerprint,json.dumps(row_object,ensure_ascii=False,default=str)))
                    if not existed: report["applications_imported"]+=1
                    else: report["duplicates_skipped"]+=1
                elif not company_id and (has_contact or has_outreach):
                    db.execute("INSERT INTO unmatched_import_rows(import_id,sheet_name,source_row,record_kind,proposed_name,match_candidates,row_json) VALUES(?,?,?,?,?,?,?)",(import_id,sheet,excel_row,"unmatched_contact_or_outreach",name,json.dumps(matches),json.dumps(row_object,ensure_ascii=False,default=str))); report["unmatched_rows"]+=1
        report["companies_inserted"]=len(inserted_company_ids)
        report["companies_updated"]=len(updated_company_ids)
        report["preview"]=preview
        db.execute("UPDATE workbook_imports SET report_json=? WHERE id=?",(json.dumps(report,ensure_ascii=False),import_id))
        db.commit()
    report["import_id"]=import_id
    return report
