import io
import json
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import research_service
from research_queue import get_company, initialize_research_schema
from research_service import batch_snapshot, discover_company_jobs, research_company, start_batch
from workbook_import import import_workbook, inspect_workbook


def xlsx_fixture():
    headers=["Company","Sector / category","Primary email domain","Email count in archive","Contact name","Contact email","Last outgoing subject","Reply / response evidence","Application status","Primary target role"]
    values=["Northstar Systems","Proposed sector","northstar.example","3","Alex Recruiter","alex@northstar.example","Introductory outreach","Awaiting response","Applied","Business Development Analyst"]
    def cell(index,value):
        n=index+1; col=""
        while n:
            n,rem=divmod(n-1,26); col=chr(65+rem)+col
        return f'<c r="{col}1" t="inlineStr"><is><t>{value}</t></is></c>'
    def row(number,items):
        vals=[]
        for index,value in enumerate(items):
            if value is None: continue
            n=index+1; col=""
            while n:
                n,rem=divmod(n-1,26); col=chr(65+rem)+col
            vals.append(f'<c r="{col}{number}" t="inlineStr"><is><t>{value}</t></is></c>')
        return f'<row r="{number}">'+"".join(vals)+"</row>"
    sheet=("<?xml version='1.0' encoding='UTF-8'?>"+
      "<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData>"+
      row(1,headers)+row(2,values)+row(3,["New Horizon Labs","Financial data",None,None,None,None,None,None,"Researching","Partnerships Analyst"])+"</sheetData></worksheet>")
    workbook="""<?xml version='1.0' encoding='UTF-8'?>
    <workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'><sheets><sheet name='Company Master' sheetId='1' r:id='rId1'/></sheets></workbook>"""
    rels="""<?xml version='1.0' encoding='UTF-8'?>
    <Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet' Target='worksheets/sheet1.xml'/></Relationships>"""
    data=io.BytesIO()
    with zipfile.ZipFile(data,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml",workbook);z.writestr("xl/_rels/workbook.xml.rels",rels);z.writestr("xl/worksheets/sheet1.xml",sheet)
    return data.getvalue()


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


class ResearchAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Path(self.temp.name)/"test.db"
        db=sqlite3.connect(self.db)
        try:
            db.executescript("""
              CREATE TABLE companies(id INTEGER PRIMARY KEY,name TEXT NOT NULL,normalized_name TEXT NOT NULL UNIQUE,sector TEXT,categories TEXT,hq_city TEXT,hq_region TEXT,hq_country TEXT,website TEXT,careers_url TEXT,source_url TEXT,verification_status TEXT,confidence TEXT,operating_locations TEXT,employee_count TEXT,size_band TEXT,stage TEXT,company_linkedin_url TEXT);
              CREATE TABLE company_sources(id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,worksheet TEXT NOT NULL,worksheet_row INTEGER NOT NULL,source_name TEXT NOT NULL,source_url TEXT,UNIQUE(worksheet,worksheet_row));
              INSERT INTO companies(id,name,normalized_name,sector,website,verification_status) VALUES(1,'Northstar Systems','northstar systems','Existing value','https://northstar.example','Unverified');
            """)
            db.commit()
        finally: db.close()
        initialize_research_schema(self.db)
    def tearDown(self): self.temp.cleanup()

    def test_company_enrichment_updates_profile_and_keeps_field_evidence(self):
        def page(url):
            return {"url":url,"status":"accessible","title":"Northstar Systems","description":"Financial technology and data platform serving investment teams.","text":"Northstar Systems is based in Zurich. Contact careers@northstar.example for recruitment information.","links":[("/careers","Careers"),("https://www.linkedin.com/company/northstar-systems","LinkedIn")] ,"json_ld":[]}
        with patch.object(research_service,"fetch_page",side_effect=page):
            result=research_company(self.db,1)
        company=get_company(self.db,1)
        self.assertEqual(result["status"],"completed")
        self.assertEqual(company["careers_url"],"https://northstar.example/careers")
        self.assertTrue(company["research_evidence"])
        self.assertEqual(company["research_evidence"][0]["source_url"],"https://northstar.example")
        self.assertEqual(company["field_states"]["careers_url"]["state"],"verified")
        self.assertEqual(len(company["contacts"]),1)

    def test_inaccessible_research_is_recorded_without_treating_fields_as_absent(self):
        from urllib.error import URLError
        with patch.object(research_service,"fetch_page",side_effect=URLError("offline")):
            result=research_company(self.db,1)
        company=get_company(self.db,1)
        self.assertEqual(result["status"],"failed")
        self.assertEqual(company["website"],"https://northstar.example")
        self.assertEqual(company["field_states"]["careers_url"]["state"],"inaccessible")
        self.assertEqual(company["source_checks"][0]["retrieval_status"],"inaccessible")

    def test_greenhouse_jobs_are_deduplicated_and_persisted(self):
        db=sqlite3.connect(self.db)
        try: db.execute("UPDATE companies SET careers_url='https://boards.greenhouse.io/northstar'");db.commit()
        finally: db.close()
        payload={"jobs":[{"title":"Business Development Analyst","absolute_url":"https://boards.greenhouse.io/northstar/jobs/123","content":"Partnerships and financial technology role","updated_at":"2026-10-22"},{"title":"Office Manager","absolute_url":"https://boards.greenhouse.io/northstar/jobs/456","content":"Facilities"}]}
        with patch.object(research_service,"urlopen",side_effect=lambda *a,**k:FakeResponse(json.dumps(payload).encode())):
            first=discover_company_jobs(self.db,1);second=discover_company_jobs(self.db,1)
        company=get_company(self.db,1)
        self.assertEqual(first["jobs_found"],1);self.assertEqual(second["jobs_found"],1)
        self.assertEqual(len(company["jobs"]),1)
        self.assertEqual(company["jobs"][0]["title"],"Business Development Analyst")

    def test_workbook_preview_imports_contacts_outreach_and_applications_additively(self):
        raw=xlsx_fixture();preview=inspect_workbook(raw)
        self.assertEqual(preview["candidate_company_rows"],2)
        report=import_workbook(self.db,raw,"fixture.xlsx")
        report_again=import_workbook(self.db,raw,"fixture.xlsx")
        company=get_company(self.db,1)
        self.assertEqual(report["contacts_imported"],1)
        self.assertEqual(report["outreach_records_imported"],1)
        self.assertEqual(report["applications_imported"],2)
        self.assertEqual(report["companies_inserted"],1)
        self.assertEqual(report["companies_updated"],1)
        self.assertTrue(report_again["already_imported"])
        self.assertEqual(len(company["contacts"]),1)
        self.assertEqual(company["sector"],"Existing value")
        self.assertEqual(company["primary_email_domain"],"northstar.example")
        self.assertEqual(len(company["outreach_history"]),1)
        self.assertEqual(len(company["applications"]),1)

    def test_batch_progress_and_retry_failed(self):
        calls=[]
        def research(_db, company_id, **kwargs):
            calls.append(company_id)
            return {"status":"failed" if len(calls)==1 else "completed","message":"controlled test result"}
        with patch.object(research_service,"research_company",side_effect=research):
            batch=start_batch(self.db,company_ids=[1])
            deadline=time.time()+4
            while time.time()<deadline:
                snapshot=batch_snapshot(self.db,batch["id"])
                if snapshot["status"]=="completed": break
                time.sleep(.03)
            self.assertEqual(snapshot["failed"],1)
            research_service.control_batch(self.db,batch["id"],"retry")
            deadline=time.time()+4
            while time.time()<deadline:
                snapshot=batch_snapshot(self.db,batch["id"])
                if snapshot["status"]=="completed" and snapshot["failed"]==0: break
                time.sleep(.03)
        self.assertEqual(snapshot["completed"],1)
        self.assertEqual(snapshot["remaining"],0)
        self.assertEqual(len(calls),2)


if __name__=="__main__": unittest.main()
