"""Provider-backed, source-preserving company research and jobs discovery."""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from research_queue import RESEARCH_FIELD_NAMES, connect, get_company, utc_now

MAX_PAGE_BYTES = 1_500_000
USER_AGENT = "CareerOpsResearch/1.0 (+public company information; low-rate)"
RELEVANT_ROLES = re.compile(r"business development|partnership|fintech|financial market|structured product|digital asset|blockchain|ai for finance|financial data|research|strategy|investment", re.I)
PAGE_HINT = re.compile(r"career|job|vacan|team|about|company|contact|product", re.I)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.text=[]; self.title=[]; self.in_title=False; self.meta={}; self.ld_json=[]; self.in_json=False; self.json_buf=[]
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag == "a" and a.get("href"): self.links.append((a["href"], a.get("aria-label") or ""))
        if tag == "title": self.in_title=True
        if tag == "meta" and (a.get("name") or a.get("property")):
            self.meta[(a.get("name") or a.get("property")).lower()]=a.get("content", "")
        if tag == "script" and a.get("type", "").lower() == "application/ld+json": self.in_json=True; self.json_buf=[]
    def handle_endtag(self, tag):
        if tag == "title": self.in_title=False
        if tag == "script" and self.in_json:
            self.ld_json.append("".join(self.json_buf)); self.in_json=False
    def handle_data(self, data):
        value=" ".join(data.split())
        if value:
            if self.in_title: self.title.append(value)
            if self.in_json: self.json_buf.append(data)
            elif len(value)>1: self.text.append(value)


def fetch_page(url: str, timeout: int = 12):
    _validate_public_url(url)
    parsed=urlparse(url)
    req=Request(url, headers={"User-Agent":USER_AGENT, "Accept":"text/html,application/xhtml+xml"})
    class SafeRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            _validate_public_url(newurl)
            return super().redirect_request(request,fp,code,msg,headers,newurl)
    with build_opener(SafeRedirect).open(req, timeout=timeout) as response:
        ctype=response.headers.get("Content-Type", "")
        if "html" not in ctype.lower() and "text/" not in ctype.lower(): return {"url":response.geturl(),"status":"inaccessible","error":"Non-HTML content type"}
        body=response.read(MAX_PAGE_BYTES+1)
        if len(body)>MAX_PAGE_BYTES: body=body[:MAX_PAGE_BYTES]
        charset=response.headers.get_content_charset() or "utf-8"
        source=body.decode(charset, errors="replace")
        parser=PageParser(); parser.feed(source)
        return {"url":response.geturl(),"status":"accessible","title":" ".join(parser.title),"description":parser.meta.get("description", "") or parser.meta.get("og:description", ""),"text":" ".join(parser.text)[:30000],"links":parser.links,"json_ld":parser.ld_json}


def _validate_public_url(url):
    parsed=urlparse(url)
    if parsed.scheme not in ("http","https") or not parsed.hostname: raise ValueError("Only public http(s) pages are supported")
    host=parsed.hostname.lower().rstrip(".")
    if host.endswith(".local"): raise ValueError("Local network URLs are not supported")
    try: addresses=[ipaddress.ip_address(host)]
    except ValueError:
        try: addresses=[ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host,parsed.port or (443 if parsed.scheme=="https" else 80),type=socket.SOCK_STREAM)]
        except socket.gaierror as exc: raise ValueError("Host could not be resolved") from exc
    if not addresses or any(not address.is_global for address in addresses): raise ValueError("Private or reserved network addresses are not supported")


def search_web(company_name: str):
    provider=os.getenv("CAREER_SEARCH_PROVIDER", "").strip().lower()
    if provider == "brave":
        key=os.getenv("BRAVE_SEARCH_API_KEY", "")
        if not key: return [], "Brave Search selected but BRAVE_SEARCH_API_KEY is not configured."
        url="https://api.search.brave.com/res/v1/web/search?q="+__import__('urllib.parse').parse.quote(company_name+" official company careers")+"&count=8"
        req=Request(url,headers={"Accept":"application/json","X-Subscription-Token":key,"User-Agent":USER_AGENT})
        try:
            with urlopen(req,timeout=12) as r: data=json.loads(r.read().decode())
            return [(x.get("url",""),x.get("title",""),x.get("description","")) for x in data.get("web",{}).get("results",[])], "Brave Search"
        except Exception as exc: return [], f"Brave Search unavailable: {type(exc).__name__}"
    if provider == "tavily":
        key=os.getenv("TAVILY_API_KEY", "")
        if not key: return [], "Tavily selected but TAVILY_API_KEY is not configured."
        body=json.dumps({"api_key":key,"query":company_name+" official website careers","search_depth":"basic","max_results":8,"include_answer":False}).encode()
        req=Request("https://api.tavily.com/search",data=body,headers={"Content-Type":"application/json","Accept":"application/json","User-Agent":USER_AGENT})
        try:
            with urlopen(req,timeout=12) as r: data=json.loads(r.read().decode())
            return [(x.get("url",""),x.get("title",""),x.get("content","")) for x in data.get("results",[])], "Tavily"
        except Exception as exc: return [], f"Tavily unavailable: {type(exc).__name__}"
    return [], "No search provider configured. Set CAREER_SEARCH_PROVIDER=brave or tavily and the corresponding Codespaces secret."


def _same_domain(url, domain):
    try: return urlparse(url).hostname == domain or urlparse(url).hostname.endswith("."+domain)
    except Exception: return False


def _domain(url): return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _company_patch(company, pages):
    fields={}; evidence=[]; contacts=[]; page_by_url={p["url"]:p for p in pages if p.get("status")=="accessible"}
    official_host=_domain(company.get("website") or "")
    if company.get("website") and pages and pages[0].get("status")=="accessible" and _same_domain(pages[0].get("url",""),official_host):
        fields["website"]=(company["website"], pages[0]["url"], .92, "Existing company master URL; official page reachable")
    for p in page_by_url.values():
        text=html.unescape(p.get("text", ""))
        if p.get("description"):
            val=p["description"].strip()
            if len(val)>15 and not company.get("sector"):
                fields.setdefault("sector", (val, p["url"], .65, "Official page meta description"))
        if not company.get("hq_country"):
            address=(re.search(r"(?:headquartered|headquarters|based)\s+(?:in|at)\s+([A-Z][A-Za-zÀ-ÿ .'-]{1,40}?)(?:,|[.;]|\b(?:and|with|where)\b|$)", text, re.I))
            if address: fields.setdefault("hq_city", (address.group(1).strip(" .,;"), p["url"], .57, address.group(0)))
        for href,label in p.get("links",[]):
            target=urljoin(p["url"],href); path=urlparse(target).path.lower()
            if re.search(r"careers|career|jobs|vacancies|open-roles", target, re.I) and _same_domain(target, official_host):
                fields.setdefault("careers_url", (target, p["url"], .88, "Official site careers link"))
            if re.search(r"linkedin\.com/company/", target, re.I) and not target.startswith("https://www.linkedin.com/company/company/"):
                fields.setdefault("company_linkedin_url", (target, p["url"], .76, "LinkedIn company link on official site"))
            if re.search(r"greenhouse\.io|jobs\.lever\.co|ashbyhq\.com", target, re.I): fields.setdefault("careers_url", (target, p["url"], .84, "Official site links to ATS"))
        emails=set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I))
        for email in emails:
            if official_host and email.lower().endswith("@"+official_host) and not re.search(r"noreply|no-reply|privacy|abuse", email, re.I):
                contacts.append({"email":email.lower(),"source_url":p["url"],"confidence":.82,"verification_status":"unverified"})
        # Public named professional contacts only when page context explicitly includes a role.
        for match in re.finditer(r"\b([A-Z][A-Za-zÀ-ÿ.'-]+\s+[A-Z][A-Za-zÀ-ÿ.'-]+)\s*[,|–-]\s*(Talent Acquisition|Recruiting|Recruitment|Chief [A-Za-z ]+|Head of [A-Za-z ]+|Managing Director|Partner)\b", text):
            contacts.append({"name":match.group(1),"title":match.group(2),"source_url":p["url"],"confidence":.61,"verification_status":"unverified"})
    return fields, contacts


def research_company(db_path, company_id, *, discover_jobs=False, force=False):
    company=get_company(db_path, company_id)
    if not company: raise ValueError("Company not found")
    if not force:
        with connect(db_path) as db:
            cached=db.execute("SELECT status,retrieved_at FROM research_runs WHERE company_id=? AND run_type='company' ORDER BY retrieved_at DESC LIMIT 1",(company_id,)).fetchone()
        if cached and cached[0]=="completed":
            try:
                checked=datetime.fromisoformat(cached[1])
                if (datetime.now(timezone.utc)-checked).total_seconds()<7*24*3600:
                    return {"status":"skipped","company_id":company_id,"cache_hit":True,"message":"A complete company research run is already cached for the last seven days."}
            except (TypeError,ValueError):
                pass
    start=utc_now(); status="partial"; messages=[]; search_results=[]; search_message=""
    seed=company.get("website")
    newly_discovered_website=False
    if not seed:
        search_results, search_message=search_web(company["name"]); messages.append(search_message)
        tokens=set(re.findall(r"[a-z0-9]+",company["name"].lower()))
        excluded=("linkedin.com","facebook.com","instagram.com","crunchbase.com","bloomberg.com","wikipedia.org","glassdoor.","indeed.")
        candidates=[]
        for u,title,snippet in search_results:
            domain=_domain(u); normalized_domain=set(re.findall(r"[a-z0-9]+",domain))
            name_hit=bool(tokens & normalized_domain) or company["name"].casefold() in (title+" "+snippet).casefold()
            if u.startswith("http") and not any(x in domain for x in excluded) and name_hit:
                candidates.append(u)
        seed=candidates[0] if candidates else None
        newly_discovered_website=bool(seed)
    pages=[]; searched=[]
    if seed:
        todo=[seed]; visited=set(); base_domain=_domain(seed)
        while todo and len(visited)<8:
            url=todo.pop(0)
            if url in visited: continue
            visited.add(url); searched.append(url)
            try:
                page=fetch_page(url); pages.append(page)
                if page.get("status")=="accessible":
                    priority=[]; other=[]
                    for href,label in page.get("links",[]):
                        target=urljoin(page["url"],href)
                        if _same_domain(target,base_domain) and urlparse(target).scheme in {"http","https"} and target not in visited:
                            (priority if PAGE_HINT.search(target+label) else other).append(target)
                    todo.extend(priority+other)
                time.sleep(.25)
            except (HTTPError,URLError,TimeoutError,ValueError,OSError) as exc:
                pages.append({"url":url,"status":"inaccessible","error":f"{type(exc).__name__}: {getattr(exc,'code','') or ''}"}); messages.append(f"Could not access {url}: {type(exc).__name__}")
    if not seed: messages.append("No official website was available and a web search provider did not return a candidate.")
    if newly_discovered_website:
        company["website"]=seed
    fields, contacts=_company_patch(company,pages)
    if newly_discovered_website and seed:
        fields["website"]=(seed,seed,.74,"Candidate official website returned by configured search and fetched")
    now=utc_now()
    protected_fields=set()
    with connect(db_path) as db:
        for field,(value,source,confidence,excerpt) in fields.items():
            current=db.execute(f"SELECT {field}, verification_status FROM companies WHERE id=?",(company_id,)).fetchone()
            field_state=db.execute("SELECT state FROM research_field_state WHERE company_id=? AND field_name=?",(company_id,field)).fetchone()
            # Do not overwrite populated, manually verified company-master data.
            protected=bool(current[0] and (str(current[1] or "").lower() in {"verified", "manually verified", "user verified"} or (field_state and field_state[0]=="verified")))
            if not protected:
                db.execute(f"UPDATE companies SET {field}=? WHERE id=?",(value,company_id))
            elif current[0] != value:
                protected_fields.add(field)
            db.execute("INSERT INTO research_evidence(company_id,field_name,field_value,source_url,retrieved_at,confidence,verification_status,source_kind,excerpt) VALUES(?,?,?,?,?,?,?,?,?)",(company_id,field,value,source,now,confidence,"verified" if field in {"website","careers_url","company_linkedin_url"} else "unverified","official_website",excerpt[:600]))
        db.execute("UPDATE research_evidence SET verification_status='inaccessible',excerpt='The stored company website value is not a valid reachable public page.' WHERE company_id=? AND field_name='website' AND source_kind='official_website' AND source_url NOT LIKE 'http://%' AND source_url NOT LIKE 'https://%'",(company_id,))
        for contact in contacts:
            db.execute("INSERT OR IGNORE INTO contacts(company_id,name,title,email,source_url,confidence,verification_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(company_id,contact.get("name"),contact.get("title"),contact.get("email"),contact.get("source_url"),contact.get("confidence",.5),contact.get("verification_status","unverified"),now,now))
        if not pages or not any(p.get("status")=="accessible" for p in pages): final_status="failed"
        elif all(p.get("status")=="accessible" for p in pages): final_status="completed"
        else: final_status="partial"
        msg="; ".join(messages)[:3000]
        run_id=db.execute("INSERT INTO research_runs(company_id,run_type,status,message,started_at,finished_at,retrieved_at) VALUES(?,?,?,?,?,?,?)",(company_id,"company",final_status,msg,start,now,now)).lastrowid
        for page in pages:
            db.execute("INSERT INTO research_source_checks(company_id,run_id,url,source_kind,retrieval_status,retrieved_at,detail) VALUES(?,?,?,?,?,?,?)",(company_id,run_id,page.get("url",""),"official_website",page.get("status","unknown"),now,page.get("error","")[:500]))
        if search_message:
            db.execute("INSERT INTO research_source_checks(company_id,run_id,url,source_kind,retrieval_status,retrieved_at,detail) VALUES(?,?,?,?,?,?,?)",(company_id,run_id,"provider://"+search_message.split(" ")[0].lower(),"web_search","completed" if search_results else "unavailable",now,search_message[:500]))
        for field_name in RESEARCH_FIELD_NAMES:
            item=fields.get(field_name)
            if item:
                state="verified" if item[2]>=.8 else "unverified"
                detail=item[3]
                if field_name in protected_fields:
                    state="verified"; detail="A newer source differs, but the protected verified profile value was preserved. Review the evidence before changing it."
            elif any(p.get("status")=="inaccessible" for p in pages):
                state="inaccessible"; detail="One or more official site pages could not be reached; field was not treated as absent."
            else:
                state="unknown"; detail="No reliable value extracted; this is not evidence that the information does not exist."
            db.execute("INSERT INTO research_field_state(company_id,field_name,state,updated_at,detail) VALUES(?,?,?,?,?) ON CONFLICT(company_id,field_name) DO UPDATE SET state=CASE WHEN excluded.state='unknown' AND research_field_state.state='verified' THEN research_field_state.state ELSE excluded.state END,updated_at=excluded.updated_at,detail=excluded.detail",(company_id,field_name,state,now,detail))
    job_result=None
    if discover_jobs: job_result=discover_company_jobs(db_path,company_id)
    return {"status":final_status,"company_id":company_id,"fields_updated":list(fields),"contacts_found":len(contacts),"pages_checked":len(pages),"pages_accessible":sum(p.get("status")=="accessible" for p in pages),"sources_searched":searched,"search_provider":search_message,"message":"; ".join(messages),"jobs":job_result}


def discover_company_jobs(db_path, company_id):
    company=get_company(db_path,company_id)
    if not company: raise ValueError("Company not found")
    with connect(db_path) as db:
        cached=db.execute("SELECT status,retrieved_at,sources_json,limitations_json,jobs_found FROM job_discovery_runs WHERE company_id=? ORDER BY retrieved_at DESC LIMIT 1",(company_id,)).fetchone()
    if cached and cached[0]=="completed":
        try:
            if (datetime.now(timezone.utc)-datetime.fromisoformat(cached[1])).total_seconds()<24*3600:
                return {"status":"skipped","cache_hit":True,"jobs_found":cached[4],"sources_searched":json.loads(cached[2]),"limitations":json.loads(cached[3])}
        except (TypeError,ValueError,json.JSONDecodeError): pass
    careers=company.get("careers_url")
    if not careers:
        # Discover ATS links from the official website by doing a small research pass.
        result=research_company(db_path,company_id,discover_jobs=False); company=get_company(db_path,company_id); careers=company.get("careers_url")
    sources=[]; openings=[]; limitations=[]
    if not careers:
        result={"status":"partial","jobs_found":0,"sources_searched":[],"limitations":["No careers page/ATS URL found; no alternate URL was fabricated."]}
        _save_job_run(db_path,company_id,result)
        return result
    parsed=urlparse(careers); host=(parsed.hostname or "").lower(); slug=(parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else "")
    api_url=None; kind=None
    if "greenhouse.io" in host:
        slug=parsed.path.strip("/").split("/")[0]; api_url=f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"; kind="Greenhouse"
    elif "lever.co" in host:
        slug=parsed.path.strip("/").split("/")[0]; api_url=f"https://api.lever.co/v0/postings/{slug}?mode=json"; kind="Lever"
    elif "ashbyhq.com" in host:
        slug=parsed.path.strip("/").split("/")[0]; api_url=f"https://api.ashbyhq.com/posting-api/job-board/{slug}"; kind="Ashby"
    else:
        try:
            page=fetch_page(careers); sources.append(careers)
            for href,label in page.get("links",[]):
                target=urljoin(careers,href)
                if RELEVANT_ROLES.search(label+target): openings.append({"title":label or target,"url":target,"description":"","location":None,"source":careers})
            limitations.append("Careers page is not a supported public ATS; only links visible in the page were inspected.")
        except Exception as exc: limitations.append(f"Careers page inaccessible: {type(exc).__name__}")
    if api_url:
        sources.append(api_url)
        try:
            req=Request(api_url,headers={"Accept":"application/json","User-Agent":USER_AGENT})
            with urlopen(req,timeout=15) as response: data=json.loads(response.read().decode("utf-8",errors="replace"))
            if kind=="Greenhouse": items=data.get("jobs",[])
            elif kind=="Lever": items=data if isinstance(data,list) else []
            else: items=data.get("jobs",[])
            for item in items:
                title=item.get("title",""); url=item.get("absolute_url") or item.get("applyUrl") or item.get("jobUrl") or item.get("url")
                if not title or not url or not RELEVANT_ROLES.search(title+" "+str(item.get("content", item.get("description","")))): continue
                description=re.sub(r"<[^>]+>"," ",item.get("content") or item.get("description") or "")
                cats=item.get("categories") or {}; location=(cats.get("location") if isinstance(cats,dict) else None) or item.get("location")
                openings.append({"title":title,"url":url,"description":html.unescape(description)[:30000],"location":location,"source":kind,"posted_at":item.get("updated_at") or item.get("createdAt")})
            active_urls={opening["url"] for opening in openings}
            with connect(db_path) as db:
                old_rows=db.execute("SELECT id,canonical_url FROM job_openings WHERE company_id=? AND source=? AND current_status='open'",(company_id,kind)).fetchall()
                for old in old_rows:
                    if old[1] not in active_urls: db.execute("UPDATE job_openings SET current_status='expired' WHERE id=?",(old[0],))
        except Exception as exc: limitations.append(f"{kind} public jobs endpoint unavailable: {type(exc).__name__}")
    now=utc_now()
    with connect(db_path) as db:
        for opening in openings:
            db.execute("INSERT INTO job_openings(company_id,title,description,location,posted_at,canonical_url,source,discovered_at,current_status) VALUES(?,?,?,?,?,?,?,?, 'open') ON CONFLICT(company_id,canonical_url) DO UPDATE SET title=excluded.title,description=excluded.description,location=excluded.location,posted_at=COALESCE(excluded.posted_at,job_openings.posted_at),source=excluded.source,discovered_at=excluded.discovered_at,current_status='open'",(company_id,opening["title"],opening["description"],opening.get("location"),opening.get("posted_at"),opening["url"],opening.get("source",kind or "careers"),now))
    result={"status":"completed" if not limitations else "partial","jobs_found":len(openings),"sources_searched":sources,"limitations":limitations}
    _save_job_run(db_path,company_id,result)
    return result


def _save_job_run(db_path,company_id,result):
    with connect(db_path) as db:
        db.execute("INSERT INTO job_discovery_runs(company_id,status,sources_json,limitations_json,jobs_found,retrieved_at) VALUES(?,?,?,?,?,?)",(company_id,result["status"],json.dumps(result.get("sources_searched",[])),json.dumps(result.get("limitations",[])),result.get("jobs_found",0),utc_now()))


def batch_snapshot(db_path, batch_id):
    with connect(db_path) as db:
        row=db.execute("SELECT * FROM batch_jobs WHERE id=?",(batch_id,)).fetchone()
        return dict(row) if row else None


def recover_interrupted_batches(db_path):
    """Checkpoint in-flight rows as queued so a Codespace restart can resume them."""
    now=utc_now()
    with connect(db_path) as db:
        db.execute("UPDATE batch_job_items SET status='queued',updated_at=? WHERE status='running' AND batch_id IN (SELECT id FROM batch_jobs WHERE status='running')",(now,))
        db.execute("UPDATE batch_jobs SET status='paused',current_company=NULL,message='Paused after application restart; resume to continue.',remaining=(SELECT count(*) FROM batch_job_items i WHERE i.batch_id=batch_jobs.id AND i.status IN ('queued','running')),updated_at=? WHERE status='running'",(now,))


_batch_controls={}


def start_batch(db_path, job_type="research", company_ids=None, retry_failed=False):
    now=utc_now(); control={"pause":threading.Event(),"stop":threading.Event()}
    with connect(db_path) as db:
        active=db.execute("SELECT id,job_type FROM batch_jobs WHERE status IN ('running','paused') ORDER BY id DESC LIMIT 1").fetchone()
        if active:
            return batch_snapshot(db_path,active[0])
        if company_ids is None:
            rows=db.execute("SELECT id FROM companies ORDER BY id").fetchall()
            ids=[r[0] for r in rows]
        else: ids=list(dict.fromkeys(int(i) for i in company_ids))
        if retry_failed:
            for row in db.execute("SELECT id,company_id FROM batch_job_items WHERE status='failed'").fetchall():
                if row[1] not in ids: ids.append(row[1])
        cur=db.execute("INSERT INTO batch_jobs(job_type,status,total,remaining,message,created_at,updated_at) VALUES(?,'running',?,?,?, ?,?)",(job_type,len(ids),len(ids),"Queued",now,now)); batch_id=cur.lastrowid
        for company_id in ids: db.execute("INSERT OR IGNORE INTO batch_job_items(batch_id,company_id,status,updated_at) VALUES(?,?,'queued',?)",(batch_id,company_id,now))
    _batch_controls[batch_id]=control
    threading.Thread(target=_run_batch,args=(db_path,batch_id,control),daemon=True).start()
    return batch_snapshot(db_path,batch_id)


def _run_batch(db_path,batch_id,control):
    while not control["stop"].is_set():
        if control["pause"].is_set():
            with connect(db_path) as db: db.execute("UPDATE batch_jobs SET status='paused',updated_at=? WHERE id=?",(utc_now(),batch_id))
            time.sleep(.25); continue
        with connect(db_path) as db:
            row=db.execute("SELECT i.id,i.company_id,c.name FROM batch_job_items i JOIN companies c ON c.id=i.company_id WHERE i.batch_id=? AND i.status='queued' ORDER BY i.id LIMIT 1",(batch_id,)).fetchone()
            if not row:
                db.execute("UPDATE batch_jobs SET status='completed',remaining=0,updated_at=? WHERE id=?",(utc_now(),batch_id)); control["stop"].set(); return
            item_id,company_id,name=row
            db.execute("UPDATE batch_job_items SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",(utc_now(),item_id))
            db.execute("UPDATE batch_jobs SET status='running',current_company=?,message=?,updated_at=? WHERE id=?",(name,"Researching "+name,utc_now(),batch_id))
        try:
            if batch_snapshot(db_path,batch_id).get("job_type")=="jobs":
                result=discover_company_jobs(db_path,company_id)
                outcome="completed" if result["status"]=="completed" else ("skipped" if result["status"]=="skipped" else "failed")
            else:
                result=research_company(db_path,company_id,discover_jobs=False)
                outcome="completed" if result["status"]=="completed" else ("skipped" if result["status"]=="skipped" else "failed")
            msg=result.get("message", "") or result.get("limitations", [""])[0] or result["status"]
        except Exception as exc: outcome="failed"; msg=f"{type(exc).__name__}: {exc}"
        with connect(db_path) as db:
            db.execute("UPDATE batch_job_items SET status=?,message=?,updated_at=? WHERE id=?",(outcome,msg[:1000],utc_now(),item_id))
            db.execute("UPDATE batch_jobs SET completed=(SELECT count(*) FROM batch_job_items WHERE batch_id=? AND status='completed'),failed=(SELECT count(*) FROM batch_job_items WHERE batch_id=? AND status='failed'),skipped=(SELECT count(*) FROM batch_job_items WHERE batch_id=? AND status='skipped'),remaining=(SELECT count(*) FROM batch_job_items WHERE batch_id=? AND status IN ('queued','running')),updated_at=? WHERE id=?",(batch_id,batch_id,batch_id,batch_id,utc_now(),batch_id))
        time.sleep(.5)


def control_batch(db_path,batch_id,action):
    control=_batch_controls.get(batch_id)
    if action=="pause" and control: control["pause"].set()
    elif action=="resume":
        if control:
            control["pause"].clear()
        else:
            control={"pause":threading.Event(),"stop":threading.Event()}; _batch_controls[batch_id]=control
            threading.Thread(target=_run_batch,args=(db_path,batch_id,control),daemon=True).start()
    elif action=="retry":
        with connect(db_path) as db:
            db.execute("UPDATE batch_job_items SET status='queued',message='',updated_at=? WHERE batch_id=? AND status='failed'",(utc_now(),batch_id))
            db.execute("UPDATE batch_jobs SET status='running',remaining=(SELECT count(*) FROM batch_job_items WHERE batch_id=? AND status IN ('queued','running')),updated_at=? WHERE id=?",(batch_id,utc_now(),batch_id))
        if not control or control["stop"].is_set():
            control={"pause":threading.Event(),"stop":threading.Event()}; _batch_controls[batch_id]=control
            threading.Thread(target=_run_batch,args=(db_path,batch_id,control),daemon=True).start()
        else: control["pause"].clear()
        return batch_snapshot(db_path,batch_id)
    with connect(db_path) as db: db.execute("UPDATE batch_jobs SET status=?,updated_at=? WHERE id=?",("paused" if action=="pause" else "running",utc_now(),batch_id))
    return batch_snapshot(db_path,batch_id)
