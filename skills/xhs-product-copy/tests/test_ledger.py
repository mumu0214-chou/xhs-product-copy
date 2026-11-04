"""Behavioral acceptance tests; all writes stay in temporary roots."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ledger.py'
spec = importlib.util.spec_from_file_location('ledger', SCRIPT)
ledger = importlib.util.module_from_spec(spec); spec.loader.exec_module(ledger)

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'ledger'
        self.n = 0
        self.start = {'event_id':'start','op':'start','task_id':'task','product':{'id':'product','name':'示例商品'},'brief':{'facts':['已确认事实'],'mandatory':['价格']},'expected':{'selections':1,'mothers':1,'variants_by_use':{'use':2}}}
        self.select = {'event_id':'select','op':'select','task_id':'task','use_id':'use','source':{'url':'https://www.xiaohongshu.com/explore/1234567890abcdef12345678?xsec_token=sample','title':'参考标题','source_text':'真实读到的完整正文','reading_scope':'full_text','read_at':'2026-01-01','provenance':'测试夹具'},'template':{'id':'pattern','name':'话题','structure':'开头到商品'},'selection':{'source_voice':'merchant','borrowed_excerpt':'正文','attraction':'具体话题','product_bridge':'匹配事实','non_transferable':'原品价格','pattern':'转场'}}
        self.mother = {'event_id':'mother','op':'drafts','task_id':'task','drafts':[{'id':'use--mother','use_id':'use','kind':'mother','expected_revision':0,'title':'母稿','body':'完整母稿正文'}]}
    def tearDown(self): self.tmp.cleanup()
    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable,str(SCRIPT),*args,'--root',str(self.root)],text=True,capture_output=True)
        if ok: self.assertEqual(p.returncode,0,p.stderr)
        else: self.assertNotEqual(p.returncode,0)
        return p
    def record(self, data, ok=True):
        self.n += 1; p = Path(self.tmp.name)/f'input-{self.n}.json';p.write_text(json.dumps(data,ensure_ascii=False))
        return self.cli('record','--input',str(p),ok=ok)
    def data(self): return json.loads((self.root/'ledger.json').read_text())
    def seed(self):
        for x in (self.start,self.select,self.mother): self.record(x)
    def variant(self,n):
        return {'event_id':f'v{n}','op':'drafts','task_id':'task','drafts':[{'id':f'use--v{n:03}','use_id':'use','kind':'variant','mother_revision':1,'expected_revision':0,'title':f'版本{n}','body':f'完整裂变正文{n}'}]}
    def test_read_empty_does_not_create(self):
        self.cli('show');self.assertFalse(self.root.exists())
    def test_retry_and_conflicting_event(self):
        self.seed();before=self.data();self.record(self.mother);self.assertEqual(self.data(),before)
        changed=copy.deepcopy(self.mother);changed['drafts'][0]['body']='变动';self.record(changed,ok=False);self.assertEqual(self.data(),before)
    def test_append_revise_and_partial(self):
        self.seed();self.record(self.variant(1));self.record({'event_id':'partial','op':'finish','task_id':'task','reason':'还差一篇'})
        self.assertEqual(self.data()['tasks']['task']['status'],'partial')
        self.record(self.variant(2));rev=self.variant(1);rev['event_id']='revise';rev['drafts'][0].update(expected_revision=1,body='修订正文');self.record(rev)
        self.record({'event_id':'finish','op':'finish','task_id':'task'})
        d=self.data();self.assertEqual(d['tasks']['task']['status'],'complete');self.assertEqual(d['uses']['use']['variants']['count'],2);self.assertEqual(len(d['drafts']['use--v001']['versions']),2)
    def test_feedback_scope_and_revision(self):
        self.seed();self.record(self.variant(1))
        self.record({'event_id':'feedback','op':'feedback','status':'approved','quote':'第一篇裂变不错','evidence':'测试用用户原话','targets':[{'kind':'draft','id':'use--v001','revision':1}]})
        d=self.data();self.assertEqual(ledger.counts(d)['approved_mothers_v2'],0)
        self.record({'event_id':'approve-mother','op':'feedback','status':'approved','quote':'母稿也采用','evidence':'测试用明确用户反馈','targets':[{'kind':'draft','id':'use--mother','revision':1}]})
        self.assertEqual(ledger.counts(self.data())['approved_mothers_v2'],1)
        self.record({'event_id':'template-review','op':'review-template','template_id':'pattern','status':'降低优先级','reason':'用户觉得这种写法重复','evidence':'测试用用户意见'})
        self.assertEqual(json.loads(self.cli('query').stdout)['templates'][0]['review_status'],'降低优先级')
        rev=copy.deepcopy(self.mother);rev['event_id']='new-mother';rev['drafts'][0].update(expected_revision=1,body='新版母稿');self.record(rev)
        self.assertEqual(self.data()['drafts']['use--v001']['versions']['1']['mother_revision'],1)
        self.assertEqual(ledger.latest_feedback(self.data(),'draft','use--mother',2)['status'],'unknown')
        self.assertEqual(ledger.counts(self.data())['approved_mothers_v2'],0)
    def test_duplicate_url_and_body(self):
        self.seed();q=copy.deepcopy(self.select);q.update(event_id='select2',use_id='use2');q['source']['url']='https://xiaohongshu.com/discovery/item/1234567890abcdef12345678?different=1';self.record(q,ok=False)
        q['source']['url']='https://xiaohongshu.com/explore/2234567890abcdef12345678';self.record(q,ok=False)
        self.assertEqual(len(self.data()['sources']),1)
    def test_invalid_batch_is_atomic(self):
        self.seed();before=self.data();q=self.variant(1);bad=copy.deepcopy(q['drafts'][0]);bad.update(id='use--v002',mother_revision=999);q['drafts'].append(bad);self.record(q,ok=False);self.assertEqual(self.data(),before)
    def test_cross_product_and_original(self):
        self.seed();q=copy.deepcopy(self.start);q.update(event_id='start2',task_id='task2');q['product']={'id':'product2','name':'另一商品'};q['brief']['facts']=['另一个价格'];q['expected']={'selections':0,'mothers':1,'variants_by_use':{}};self.record(q)
        self.record({'event_id':'original','op':'original','task_id':'task2','use_id':'original-use','topic':'直接原创'})
        q=copy.deepcopy(self.mother);q.update(event_id='cross',task_id='task2');self.record(q,ok=False)
        h=json.loads(self.cli('handoff','--task','task2').stdout);self.assertEqual(h['task']['brief']['facts'],['另一个价格']);self.assertEqual(h['sources'],{})
    def test_v1_migration_retains_history(self):
        self.root.mkdir();old={'schema_version':1,'sources':{},'templates':{},'products':{'p':{'id':'p','name':'旧商品'}},'uses':{'old':{'id':'old','product_id':'p','source_id':None,'template_id':None,'rewrite':{'status':'done','output_location':'聊天'},'variants':{'status':'done','count':10},'feedback':{'status':'approved','target':'旧裂变组'}}},'events':[]}
        (self.root/'ledger.json').write_text(json.dumps(old));self.cli('migrate');d=self.data();self.assertEqual(d['uses'],old['uses']);self.assertEqual(ledger.counts(d)['variants'],10);self.assertEqual(d['drafts'],{});self.assertTrue((self.root/'ledger.v1.backup.json').exists())
    def test_concurrent_no_lost_updates_and_export(self):
        self.seed();jobs=[]
        for n in range(1,9):
            p=Path(self.tmp.name)/f'parallel{n}.json';p.write_text(json.dumps(self.variant(n)));jobs.append(subprocess.Popen([sys.executable,str(SCRIPT),'record','--root',str(self.root),'--input',str(p)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        for j in jobs:
            out,err=j.communicate();self.assertEqual(j.returncode,0,err)
        d=self.data();self.assertEqual(d['uses']['use']['variants']['count'],8)
        dest=Path(self.tmp.name)/'export';self.cli('export','--output',str(dest));self.assertEqual(json.loads((dest/'ledger.json').read_text()),d)
        for draft in d['drafts'].values():
            for v in draft['versions'].values():self.assertTrue((dest/v['artifact']).exists())
    def test_artifact_recovery_and_tamper_detection(self):
        self.seed();d=self.data();p=self.root/d['drafts']['use--mother']['versions']['1']['artifact'];p.unlink();self.cli('render');self.assertTrue(p.exists());p.write_text('外部修改');self.record(self.variant(1),ok=False);self.assertEqual(self.data(),d)
    def test_query_excludes_selected_before_written(self):
        self.record(self.start);self.record(self.select);q=json.loads(self.cli('query').stdout);self.assertIn('1234567890abcdef12345678',q['exclude_source_ids']);self.assertEqual(q['templates'][0]['mother_uses'],0)
    def test_publication_requires_evidence_and_is_version_scoped(self):
        self.seed();self.record({'event_id':'pub-bad','op':'publication','draft_id':'use--mother','revision':1},ok=False)
        self.record({'event_id':'pub','op':'publication','draft_id':'use--mother','revision':1,'evidence':'用户给出的发布证据（隔离测试）'})
        q=copy.deepcopy(self.mother);q['event_id']='revise-after-publish';q['drafts'][0].update(expected_revision=1,body='新稿尚未发布');self.record(q)
        v=self.data()['drafts']['use--mother']['versions'];self.assertEqual(v['1']['publication']['status'],'published');self.assertNotIn('publication',v['2'])
    def test_withdraw_replace_and_adjust_plan(self):
        self.record(self.start);self.record(self.select)
        self.record({'event_id':'withdraw','op':'withdraw','task_id':'task','use_id':'use','reason':'当前产品无法承接'})
        self.record(self.mother,ok=False)
        d=self.data();self.assertEqual(ledger.progress(d,'task')['actual']['selections'],0);self.assertEqual(len(d['sources']),1)
        q=copy.deepcopy(self.select);q.update(event_id='replace',use_id='use2');q['source']['url']='https://www.xiaohongshu.com/explore/3234567890abcdef12345678';q['source']['source_text']='另一篇实际正文';self.record(q)
        self.record({'event_id':'plan','op':'plan','task_id':'task','expected':{'selections':1,'mothers':1,'variants_by_use':{'use2':2}},'reason':'替换不适配参考，原数量不变'})
        self.assertEqual(ledger.progress(self.data(),'task')['expected']['variants_by_use'],{'use2':2})
    def test_stale_revision_and_no_false_publication(self):
        self.seed();self.record(self.variant(1));q=self.variant(1);q['event_id']='stale';self.record(q,ok=False)
        d=self.data();self.assertEqual(d['uses']['use']['publication']['status'],'unknown');self.assertNotIn('publication',d['drafts']['use--mother']['versions']['1'])

if __name__=='__main__': unittest.main(verbosity=2)
