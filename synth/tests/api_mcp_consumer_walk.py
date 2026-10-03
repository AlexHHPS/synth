"""Independent REST and official MCP SDK consumer. No paid calls or audio exports.
Uses completed meetings and a temporary scoped read-only integration, revoked
in finally. Does not alter transcripts, documents, notes, or employee profiles.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.error
from uuid import uuid4
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT=Path(__file__).resolve().parents[2]
BASE=os.environ.get('SYNTH_TEST_API_URL','http://127.0.0.1:18280')
OWNER=os.environ.get('SYNTH_TEST_OWNER_TOKEN') or (Path.home()/'Library/Application Support/dev.synth.voice/desktop-api-key').read_text().strip()
DEMO=os.environ['SYNTH_TEST_MEETING_ID']
PRIVATE=os.environ['SYNTH_TEST_PRIVATE_MEETING_ID']
checks=[]

def call(method,path,token=None,body=None,expected=200,headers=None):
    h={'Content-Type':'application/json','Accept':'application/json, text/event-stream',**(headers or {})}
    if token is not None:h['Authorization']='Bearer '+token
    req=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers=h)
    try:r=urllib.request.urlopen(req,timeout=20)
    except urllib.error.HTTPError as e:r=e
    assert r.status==expected,(method,path,r.status,expected)
    raw=r.read();return json.loads(raw) if raw else None

def rpc(method,token=None,params=None,expected=200):
    return call('POST','/mcp',token,{'jsonrpc':'2.0','id':1,'method':method,'params':params or {}},expected)

async def sdk_walk(key,rest):
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+key},timeout=20) as http:
        async with streamable_http_client(BASE+'/mcp',http_client=http) as (read,write,_):
            async with ClientSession(read,write) as session:
                init=await session.initialize();checks.append('official_mcp_sdk_initialize_'+init.protocolVersion)
                await session.send_ping();tools=await session.list_tools()
                names={t.name for t in tools.tools}
                assert names=={'list_folders','list_meetings','get_meeting','get_transcript','get_document','get_summary','get_notes','list_versions','get_processing_status','search_transcripts'}
                checks.append('official_mcp_sdk_discovery_10_tools')
                for name,path,args in [('get_meeting','',{'meeting_id':DEMO}),('get_transcript','/transcript',{'meeting_id':DEMO}),('get_document','/document',{'meeting_id':DEMO}),('get_summary','/summary',{'meeting_id':DEMO}),('get_notes','/notes',{'meeting_id':DEMO}),('list_versions','/versions',{'meeting_id':DEMO})]:
                    result=await session.call_tool(name,args);assert not result.isError
                    assert json.loads(result.content[0].text)==rest[path], name
                    checks.append('sdk_rest_parity_'+name)
                status=await session.call_tool('get_processing_status',{'meeting_id':DEMO});assert not status.isError;assert json.loads(status.content[0].text)==rest['/jobs'];checks.append('sdk_processing_status_by_meeting')
                job_id=rest['/jobs']['items'][0]['id'];status=await session.call_tool('get_processing_status',{'job_id':job_id});assert not status.isError;assert json.loads(status.content[0].text)==call('GET','/v1/jobs/'+job_id,key);checks.append('sdk_processing_status_by_job')
                denied=await session.call_tool('get_processing_status',{'meeting_id':PRIVATE});assert denied.isError;checks.append('sdk_private_processing_status_denied')
                folders=await session.call_tool('list_folders',{});assert not folders.isError
                meetings=await session.call_tool('list_meetings',{'folder_id':rest['']['folder_id'],'limit':1});assert not meetings.isError
                assert json.loads(meetings.content[0].text)['items']
                checks.extend(['sdk_scoped_folders','sdk_folder_meetings_pagination'])
                search=await session.call_tool('search_transcripts',{'q':rest['/transcript']['content']['segments'][0]['text'].split()[0]});assert not search.isError
                checks.append('sdk_search')
                for name in ['get_meeting','get_transcript','get_document','get_summary','get_notes','list_versions']:
                    denied=await session.call_tool(name,{'meeting_id':PRIVATE});assert denied.isError
                checks.append('sdk_private_meeting_denied_all_tools')
                for name,args in [('get_audio',{'meeting_id':DEMO}),('get_voice_profile',{}),('get_transcript',{'meeting_id':DEMO,'version':0}),('get_meeting',{'meeting_id':DEMO,'unexpected':True})]:
                    denied=await session.call_tool(name,args);assert denied.isError
                checks.append('sdk_audio_profiles_and_invalid_arguments_denied')

def main():
    meeting=call('GET','/v1/meetings/'+DEMO,OWNER)
    issued=call('POST','/v1/integrations',OWNER,{'name':'API MCP verification '+str(uuid4())[:8],'folder_ids':[meeting['folder_id']]},201)
    key=issued['token']
    try:
        paths=['','/transcript','/document','/summary','/notes','/versions','/jobs']
        rest={}
        for suffix in paths:
            route='/v1/meetings/'+DEMO+suffix
            rest[suffix]=call('GET',route,key); checks.append('key_allowed_'+(suffix or 'metadata'))
            call('GET',route,None,expected=401);checks.append('no_key_401_'+(suffix or 'metadata'))
            call('GET',route,'invalid-test-key',expected=401)
            call('GET','/v1/meetings/'+PRIVATE+suffix,key,expected=404)
        assert rest['/document']==rest['/summary']
        call('GET','/v1/meetings/'+DEMO+'/transcript?version='+str(rest['/transcript']['version']),key)
        call('GET','/v1/meetings/'+DEMO+'/document?version='+str(rest['/document']['version']),key)
        checks.extend(['invalid_key_401_all_reads','foreign_folder_404_all_reads','versioned_reads','summary_document_parity'])
        call('PUT','/v1/meetings/'+DEMO+'/notes',key,{'notes':'must not be written','expected_revision':rest['']['revision']},403)
        checks.append('integration_write_denied')
        for method,params in [('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'consumer-test','version':'1'}}),('tools/list',{}),('tools/call',{'name':'get_transcript','arguments':{'meeting_id':DEMO}})]:
            rpc(method,None,params,401);rpc(method,'invalid-test-key',params,401)
        checks.extend(['mcp_no_key_401','mcp_invalid_key_401'])
        call('GET','/mcp',None,expected=401);call('GET','/mcp',key,expected=405)
        call('POST','/mcp',key,{'jsonrpc':'2.0','id':1,'method':'ping'},403,{'Origin':'https://untrusted.example'})
        call('POST','/mcp',key,{'jsonrpc':'2.0','id':1,'method':'ping'},400,{'MCP-Protocol-Version':'unsupported'})
        checks.extend(['mcp_get_requires_auth','mcp_stateless_get_405','mcp_untrusted_origin_403','mcp_unsupported_protocol_400'])
        asyncio.run(sdk_walk(key,rest))
        call('DELETE','/v1/integrations/'+issued['id'],OWNER)
        call('GET','/v1/meetings/'+DEMO+'/transcript',key,expected=401)
        rpc('tools/list',key,expected=401);checks.extend(['revoked_key_rest_401','revoked_key_mcp_401'])
        report={'status':'PASS','mode':'real_http_plus_official_mcp_sdk','sdk':'mcp==1.30.0','data':'existing_completed_meetings_no_new_llm_calls','checks':checks,'document_hash':rest['/document']['content_hash'],'transcript_hash':rest['/transcript']['content_hash'],'transcript_segments':len(rest['/transcript']['content']['segments']),'integration_revoked':True,'audio_and_voice_vectors_exported':False,'acceptance_rows_closed':[]}
        report['base_url']=BASE
        name='api-mcp-consumer-cloud.json' if BASE.startswith('https:') else 'api-mcp-consumer.json'
        (ROOT/'synth/.runtime/proof').mkdir(parents=True,exist_ok=True)
        (ROOT/'synth/.runtime/proof'/name).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
    finally:
        call('DELETE','/v1/integrations/'+issued['id'],OWNER)

if __name__=='__main__':main()
