from types import SimpleNamespace
from backend.domain.facts import *
from backend.services.fact_service import *
from backend.domain.rag import DocumentChunk, RetrievedEvidence, EvidenceSource
from backend.domain.search import SourceType, BodyStatus, PriceEvidenceLevel

def test_adapt_retrieved_and_confirm_idempotent():
 c=DocumentChunk('c1','d1','型号 X 速度 100',EvidenceSource.USER_DOCUMENT,page=2,location='page:2')
 e=evidence_from_retrieved(RetrievedEvidence(c,1.0), 'cand')
 f=make_fact('cand','speed',100,'number',e,unit='rpm')
 assert confirm_fact(confirm_fact(f,'u'),'u').status is FactStatus.CONFIRMED

def test_source_and_tender_fields():
 s=SimpleNamespace(url='https://x.test/tender.pdf',title='中标公告',source_type=SourceType.TENDER_DOCUMENT,authority_score=85,body_status=BodyStatus.FETCHED,body_text='成交价 100',price_evidence_level=PriceEvidenceLevel.HISTORICAL_TENDER,tender_title='2025采购',publication_date='2025-01-01',currency='CNY',tax_condition='含税',source_location='page:3')
 e=evidence_from_source(s,'c'); assert e.price_evidence_level=='historical_tender' and e.page is None and e.body_location=='page:3'

def test_snippet_and_conflict():
 c=SimpleNamespace(chunk_id='x',document_id='d',text='摘要',source_type='search_snippet',location='body',source_url='https://x',authority_score=5,evidence_status='available',page=None)
 try: evidence_from_retrieved(RetrievedEvidence(c,1),'c')
 except ValueError: pass
 else: assert False
 e1=FactEvidence('e1','c','user_document',90,FactEvidenceLevel.USER_PROVIDED,__import__('datetime').datetime.now(),document_id='d',location='page:1')
 e2=FactEvidence('e2','c','official_source',90,FactEvidenceLevel.PRIMARY,__import__('datetime').datetime.now(),url='https://x',body_location='body')
 fs=reconcile_facts([make_fact('c','x',1,'number',e1,fact_id='f1'),make_fact('c','x',2,'number',e2,fact_id='f2')])
 assert all(f.status is FactStatus.CONFLICTED for f in fs)
