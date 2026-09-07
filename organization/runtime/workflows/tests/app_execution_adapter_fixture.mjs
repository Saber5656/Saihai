import assert from 'node:assert/strict';
import {createAppHostAdapter} from '../scripts/app_execution.mjs';
const names=['list_projects','create_thread','list_threads','read_thread','send_message_to_thread','wait_threads'];
let calls=[];const raw={content:[{type:'text',text:'{"clientThreadId":"client-pending"}'}]};
const hostTools=Object.fromEntries(names.map(n=>[n,async args=>{calls.push([n,args]);return raw;}]));
let received;const journal={next:async()=>({status:'invoke',token:'claimed',method:'create_thread',args:{target:'fixture'}}),
  accept:async(token,result)=>{received=result;return {status:'pending'};},lost:async()=>({status:'unknown'})};
const adapter=createAppHostAdapter({hostTools,journal});
hostTools.create_thread=()=>{throw new Error('replacement must not execute');};
assert.equal((await adapter.step('op')).status,'pending');assert.equal(received,raw);assert.equal(calls.length,1);
journal.next=async()=>({status:'waiting',reason:'pending'});await adapter.step('op');assert.equal(calls.length,1);
journal.next=async()=>({status:'invoke',token:'claimed',method:'shell',args:{}});
await assert.rejects(adapter.step('op'),/unsupported_host_tool/);
assert.deepEqual(Object.keys(adapter),['step']);
console.log('supported callback capture / pending no retry / arbitrary method rejection pass');
