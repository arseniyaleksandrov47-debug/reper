"""Bounded reuse of completed premises proofs; source freshness remains separate."""
import asyncio
import copy
import hashlib
import json
import time
from collections import OrderedDict

from .assessment_cache import AssessmentCache


class ConfirmationProofCache:
    def __init__(self,path=None,*,ttl=240,max_entries=256):
        self.ttl=ttl;self.max_entries=max_entries
        self.memory=OrderedDict();self.flights={}
        self.disk=AssessmentCache(path,max_entries=1024) if path else None

    @staticmethod
    def key(concept,catalog,key,claim,primary_model,proof_model):
        # Timestamp changes alone don't change the evidence. A hit still needs
        # the caller's current source snapshot to be fresh at the decision.
        facts={k:{n:v for n,v in fact.items() if n!='fetched_at'} for k,fact in catalog.items()}
        payload=dict(revision=3,concept=concept,facts=facts,check=key,
                     claim=dict(result=claim['result'],evidence_ids=sorted(set(claim['evidence_ids']))),
                     models=[primary_model,proof_model])
        return 'premises_proof:'+hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

    @staticmethod
    def valid(value,model):
        if not isinstance(value,(list,tuple)) or len(value)!=2 or value[1]!=model:return False
        proof=value[0]
        return (isinstance(proof,dict) and proof.get('verdict') in ('supported','unsupported') and
                isinstance(proof.get('reason'),str) and bool(proof['reason'].strip()) and len(proof['reason'])<=220 and
                ('reason_full' not in proof or isinstance(proof['reason_full'],str) and len(proof['reason_full'])<=2000) and
                not proof.get('error_type') and set(proof)<={'verdict','reason','reason_full'})

    @staticmethod
    def result(value,status):
        proof,model=copy.deepcopy(value)
        proof['reuse']=dict(hit=status!='fresh',source=status)
        return proof,model

    async def get_or_compute(self,key,compute,*,model,valid_until):
        now=time.time();hit=self.memory.get(key)
        if hit and min(hit[0],valid_until)>now and self.valid(hit[1],model):
            self.memory.move_to_end(key);return self.result(hit[1],'memory')
        self.memory.pop(key,None)
        if self.disk:
            stored=await asyncio.to_thread(self.disk.get,key)
            if stored and min(stored[0],valid_until)>time.time() and self.valid(stored[1],model):
                self.memory[key]=stored
                while len(self.memory)>self.max_entries:self.memory.popitem(last=False)
                return self.result(stored[1],'disk')
        flight=self.flights.get(key)
        if flight is None:
            async def load():
                value=await compute()
                expires=min(time.time()+self.ttl,valid_until)
                if expires>time.time() and self.valid(value,model):
                    self.memory[key]=(expires,copy.deepcopy(value));self.memory.move_to_end(key)
                    while len(self.memory)>self.max_entries:self.memory.popitem(last=False)
                    if self.disk:
                        await asyncio.to_thread(self.disk.put,key,value,max(0,expires-time.time()))
                return value
            flight=dict(task=asyncio.create_task(load()),users=0)
            self.flights[key]=flight
        flight['users']+=1
        try:return self.result(await asyncio.shield(flight['task']),'fresh')
        finally:
            flight['users']-=1
            if not flight['users']:
                if not flight['task'].done():flight['task'].cancel()
                if self.flights.get(key) is flight:self.flights.pop(key,None)
