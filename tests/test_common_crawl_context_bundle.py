"""Run the real context path using only the installer's declared bundle files."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Keep the subprocess isolated from the repository and site packages so lazy
# imports must be provided by the actual deployment inventory.
PROBE = r'''
import hashlib,json,pathlib,socket,sys
from datetime import datetime,timezone
bundle=pathlib.Path(sys.argv[1]); scratch=pathlib.Path(sys.argv[2])
sys.path.insert(0,str(bundle))
def no_network(*args,**kwargs): raise AssertionError('network access forbidden')
socket.socket.connect=no_network
from collectors import common_crawl_lake as lake
from core.governance import KillSwitch
from scripts import common_crawl_lake as cli
warehouse=scratch/'warehouse'
url='https://www.stats.gov.cn/sj/zxfb/'
row={'crawl':'CC-MAIN-2026-30','url':url,'url_host_name':'www.stats.gov.cn',
     'fetch_time':'2026-07-24T12:30:00Z','fetch_status':200,'content_digest':'A'*32,
     'content_mime_detected':'text/html','content_languages':'zho,eng',
     'warc_filename':'crawl-data/CC-MAIN-2026-30/segments/1780000000000.1/warc/CC-MAIN-2026-30-00000.warc.gz',
     'warc_record_offset':100,'warc_record_length':512}
export=scratch/'fixture.jsonl';export.write_text(json.dumps(row)+'\n')
config=bundle/'config/common_crawl_targets.json'
lake.ingest_export(export,config_path=config,warehouse=warehouse,kill_switch=KillSwitch(path=scratch/'halt'),now=datetime(2026,8,12,tzinfo=timezone.utc))
news=scratch/'news.json';osint=scratch/'osint.json'
news.write_text(json.dumps({'schema_version':'palimpsest-newswire.v1','generated_at':'2026-08-20T00:00:00Z',
    'events':[{'event_id':'event-'+'1'*24,'version_id':'eventv-'+'2'*24,'published_at':'2026-08-19T12:00:00Z',
               'topics':['economy'],'evidence_strength':'single-source','evidence_groups':[{'group_id':'one'}],
               'evidence_refs':[{'source_id':'fixture'}],
               'declared_links':{'relation':'topic-surface-only','scan_signal_ids':[],'economic_signal_ids':[]}}]}))
osint.write_text(json.dumps({'schema_version':'osint-china.v1','generated_at':'2026-08-20T00:00:00Z',
    'signals':[{'id':'undertext','layer':'archive','payload':{'observations':[{'source':'undertext:fusion:wayback','url':url,'title':'NBS release'}]}}]}))
database=warehouse/lake.DEFAULT_DATABASE_NAME
before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (database,export,news,osint)}
args=cli.build_parser().parse_args(['--config',str(config),'--warehouse',str(warehouse),'refresh','--newswire',str(news),'--osint',str(osint),'--now','2026-08-20T01:00:00Z'])
result=cli.run(args)
assert result['status']=='success',result
assert result['context']['china_joins']['matches']==1,result
joins=warehouse/'derived'/lake.CHINA_JOINS_FILENAME
receipt=json.loads(joins.read_text())
assert receipt['n_matches']==1 and receipt['matches'][0]['observation_key']
assert receipt['matches'][0]['match_kind']=='url'
assert joins.stat().st_mode & 0o777 == 0o600
for forbidden in ('https://www.stats.gov.cn','warc_filename','warc_record_offset','warc_record_length','NBS release'):
    assert forbidden not in joins.read_text(),forbidden
assert {name:hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest() for name in before}==before
from core.china_observation import load_gazetteer_index
index,terms=load_gazetteer_index()
assert index and terms,'Packaged gazetteer silently fell back to empty'
print(json.dumps({'status':'PASS','matches':receipt['n_matches'],'gazetteer_terms':len(terms),'input_bytes_unchanged':True,'network_calls':0}))
'''


def stage_bundle(tmp_path):
    installer=(ROOT/'ops/common-crawl/install-host-bundle.sh').read_text()
    declaration=re.search(r'^bundle_files=\((.*?)^\)',installer,re.M|re.S)
    assert declaration
    files=re.findall(r'"([^":]+):([^":]+):([0-7]+)"',declaration.group(1))
    bundle=tmp_path/'bundle';bundle.mkdir()
    for source,destination,mode in files:
        path=bundle/destination;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((ROOT/source).read_bytes());path.chmod(int(mode,8))
    # Check the independently listed checksum inputs cover the packaged files.
    checksum=re.search(r'cd "\$bundle_tmp"\s+sha256sum\s+(.*?)>MANIFEST.sha256',installer,re.S).group(1)
    assert {destination for _,destination,_ in files} <= set(checksum.replace('\\',' ').split())
    return bundle


def run_probe(bundle, scratch):
    scratch.mkdir()
    return subprocess.run([sys.executable,'-I','-S','-B','-c',PROBE,str(bundle),str(scratch)],
                          cwd=scratch,env={'PATH':os.environ.get('PATH','/usr/bin:/bin')},
                          capture_output=True,text=True,timeout=60)


def test_declared_bundle_runs_full_refresh_and_sanitized_china_join(tmp_path):
    bundle=stage_bundle(tmp_path)
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in bundle.rglob('*') if p.is_file()}
    result=run_probe(bundle,tmp_path/'state')
    assert result.returncode==0,result.stdout+result.stderr
    proof=json.loads(result.stdout)
    assert proof['matches']==1 and proof['input_bytes_unchanged'] and proof['gazetteer_terms']>0
    assert {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in bundle.rglob('*') if p.is_file()}==before


@pytest.mark.parametrize('missing',['core/china_observation.py','config/zh_censorship_gazetteer.json'])
def test_full_refresh_fixture_exposes_missing_lazy_dependency_or_resource(tmp_path,missing):
    bundle=stage_bundle(tmp_path)
    (bundle/missing).unlink()
    result=run_probe(bundle,tmp_path/'state')
    assert result.returncode!=0
    assert ('core.china_observation' if missing.endswith('.py') else 'gazetteer silently fell back') in result.stderr
