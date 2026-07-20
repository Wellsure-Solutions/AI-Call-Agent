import asyncio
import json
from pathlib import Path
from app.core.models import CallSession
from app.services.call_service import CallResultService
from app.storage.sqlite_store import SQLiteCallStore, SuppressedError


def store(tmp_path): return SQLiteCallStore(tmp_path/'calls.db', tmp_path)

def lead(repo, phone='+14155552671'):
    return repo.import_leads([{'business_name':'Acme','phone_number':phone,'category':'Retail','notes':''}])['leads'][0]

def test_two_repositories_and_global_claim_are_atomic(tmp_path):
    one=store(tmp_path); item=lead(one); call=one.enqueue_call(phone_number=item['phone_number'],lead_id=item['lead_id'])
    two=SQLiteCallStore(tmp_path/'calls.db',tmp_path)
    assert one.claim_job('worker-a',1)['call_id']==call['call_id']
    assert two.claim_job('worker-b',1) is None

def test_terminal_before_media_and_duplicate_terminal_are_idempotent(tmp_path):
    repo=store(tmp_path); item=lead(repo); call=repo.enqueue_call(phone_number=item['phone_number'],lead_id=item['lead_id'])
    repo.bind_call_sid(call['call_id'],'CA1'); repo.provider_status(call['call_id'],'busy','CA1'); finalized=repo.get_call(call['call_id'])['finalized_at']
    repo.provider_status(call['call_id'],'busy','CA1')
    assert repo.claim_media(call['call_id'],'CA1','worker') is None
    assert repo.get_call(call['call_id'])['finalized_at']==finalized

def test_raw_survives_extraction_failure_and_lead_leaves_calling(tmp_path):
    class Broken:
        def extract(self, session):
            assert repo.get_call(session.call_id)['transcript']
            raise TimeoutError('secret provider detail')
    repo=store(tmp_path); item=lead(repo); call=repo.enqueue_call(phone_number=item['phone_number'],lead_id=item['lead_id'])
    session=CallSession(call_id=call['call_id'],phone_number=item['phone_number'],metadata={'lead_id':item['lead_id'],'media_connected':True}); session.add_turn('user','partial words')
    asyncio.run(CallResultService(Broken(),repo,timeout=.1,max_attempts=1).afinalize(session))
    saved=repo.get_call(call['call_id'])
    assert 'partial words' in saved['transcript'] and saved['extraction_status']=='failed'
    assert repo.get_lead(item['lead_id'])['status']!='calling'

def test_current_mapping_and_dnd_suppresses_future_calls(tmp_path):
    repo=store(tmp_path); item=lead(repo); call=repo.enqueue_call(phone_number=item['phone_number'],lead_id=item['lead_id'])
    repo.save_extraction(call['call_id'],{'account_creation_interest':'maybe','callback_approved':'yes','do_not_call_requested':'yes'})
    saved=repo.get_call(call['call_id']); assert saved['interested']==0 and saved['callback_requested']==1
    assert saved['structured_response']['account_creation_interest']=='maybe'
    try: repo.enqueue_call(phone_number=item['phone_number'],lead_id=item['lead_id'])
    except SuppressedError: pass
    else: raise AssertionError('suppressed phone was callable')

def test_legacy_migration_is_idempotent_and_source_untouched(tmp_path):
    source=[{'lead_id':'legacy','business_name':'Legacy','phone_number':'+14155550000','status':'new'}]
    path=tmp_path/'leads.json'; path.write_text(json.dumps(source)); before=path.read_bytes()
    store(tmp_path); store(tmp_path)
    assert path.read_bytes()==before
    assert len(SQLiteCallStore(tmp_path/'calls.db',tmp_path).list_leads())==1
