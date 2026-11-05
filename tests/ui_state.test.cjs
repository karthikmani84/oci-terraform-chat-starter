const test = require('node:test');
const assert = require('node:assert/strict');
const {controlsFor, confirmationMatches, summaryCounts, requestHeaders, responseError, reviewVisible} = require('../app/static/app.js');
const now = Date.parse('2026-11-03T18:00:00Z');
const plan = {id:'plan-actual-1',expires_at:'2026-11-03T18:15:00Z',summary:{create:1,update:0,delete:0}};

test('code edits cannot reuse review of a prior revision', () => {
  const controls=controlsFor({stage:'review',revision:4,reviewed_revision:3},now);
  assert.equal(controls.canReview,true); assert.equal(controls.canPlan,false); assert.equal(controls.canApply,false);
});
test('confirmed current code review unlocks Plan without unlocking Apply', () => {
  const controls=controlsFor({stage:'review',revision:4,reviewed_revision:4},now);
  assert.equal(controls.canPlan,true); assert.equal(controls.canReview,false); assert.equal(controls.canApply,false);
});
test('a saved, unexpired, planned ID is needed for Apply', () => {
  assert.equal(controlsFor({stage:'planned',plan},now).canApply,true);
  for(const stage of ['clarify','review','planning','drift','applying','result','error'])
    assert.equal(controlsFor({stage,plan},now).canApply,false,stage);
  assert.equal(controlsFor({stage:'planned',plan:{expires_at:plan.expires_at}},now).canApply,false);
});
test('Plan expires exactly at its deadline and a new Plan becomes available', () => {
  const controls=controlsFor({stage:'planned',plan},Date.parse(plan.expires_at));
  assert.equal(controls.canApply,false); assert.equal(controls.canPlan,true);
});
test('missing or malformed expiry never enables Apply', () => {
  for(const expires_at of [undefined,'not-a-date',null,'']) assert.equal(controlsFor({stage:'planned',plan:{id:'x',expires_at}},now).canApply,false);
});
test('in-flight work locks all mutation controls', () => {
  for(const stage of ['review','planned','drift']) {
    const controls=controlsFor({stage,busy:true,revision:2,reviewed_revision:2,plan},now);
    assert.equal(controls.canReview||controls.canPlan||controls.canApply||controls.canResolveDrift,false,stage);
  }
});
test('deletes and replacements need the exact second confirmation phrase', () => {
  const destructive={...plan,destructive:true,confirmation_phrase:'APPLY plan-actual-1'};
  for(const typed of ['', 'apply plan-actual-1','APPLY plan-other','APPLY plan-actual-1 ']) assert.equal(confirmationMatches(destructive,typed),false);
  assert.equal(confirmationMatches(destructive,'APPLY plan-actual-1'),true);
  assert.equal(confirmationMatches({...destructive,destructive:false},destructive.confirmation_phrase),false);
});
test('a destructive plan without a phrase cannot pass confirmation', () => {
  assert.equal(confirmationMatches({...plan,destructive:true},''),false);
});
test('drift choices are available only in the explicit drift stage', () => {
  assert.equal(controlsFor({stage:'drift'},now).canResolveDrift,true);
  assert.equal(controlsFor({stage:'planned',drift:{summary:'changed'},plan},now).canResolveDrift,false);
});
test('zero change counts remain visible rather than disappearing', () => {
  assert.deepEqual(summaryCounts({create:0,update:0,delete:0}),{create:0,update:0,delete:0,replace:null});
});

test('actual Terraform driver nested counts render add/change/destroy totals', () => {
  assert.deepEqual(summaryCounts({counts:{add:1,change:2,destroy:0,replace:0,import:1},resources:[]}),{create:1,update:2,delete:0,replace:0});
});

test('a failed Plan can be retried only while the current revision remains reviewed', () => {
  assert.equal(controlsFor({stage:'error',revision:3,reviewed_revision:3},now).canPlan,true);
  assert.equal(controlsFor({stage:'error',revision:3,reviewed_revision:2},now).canPlan,false);
  assert.equal(controlsFor({stage:'error',revision:3,reviewed_revision:3},now).canApply,false);
});

test('Gateway and app CSRF values use distinct headers on POST only', () => {
  const config={csrf_token:'fake-app-csrf'};
  assert.deepEqual(requestHeaders(config,'fake-gateway-csrf','POST',true),{Accept:'application/json','Content-Type':'application/json','X-Chat-CSRF':'fake-app-csrf','X-CSRF-TOKEN':'fake-gateway-csrf'});
  assert.deepEqual(requestHeaders(config,'fake-gateway-csrf','GET',false),{Accept:'application/json'});
  assert.equal(requestHeaders(config,'','POST',true)['X-CSRF-TOKEN'],undefined);
});
test('public application 403 cause remains visible instead of a misleading sign-in error', () => {
  assert.equal(responseError(403,{error:'Refresh the page to renew this session.'}),'Refresh the page to renew this session.');
  assert.match(responseError(403,{error:{private:'not-public'}}),/sign-in/);
  assert.match(responseError(401,{error:'Unauthenticated'}),/sign-in/);
});

test('unchanged managed code still exposes review then Plan for a live drift check', () => {
  const c={stage:'review',revision:2,diff:'',reviewed_revision:null};
  assert.equal(reviewVisible(c),true);
  assert.equal(controlsFor(c,now).canReview,true);
  assert.equal(controlsFor(c,now).canPlan,false);
  assert.equal(controlsFor(c,now).canApply,false);
  c.reviewed_revision=2;
  assert.equal(reviewVisible(c),true);
  assert.equal(controlsFor(c,now).canReview,false);
  assert.equal(controlsFor(c,now).canPlan,true);
  assert.equal(controlsFor(c,now).canApply,false);
});
test('an empty clarification has no review card and a reviewed unchanged Plan retry keeps it', () => {
  assert.equal(reviewVisible({stage:'clarify',diff:''}),false);
  assert.equal(reviewVisible({stage:'error',diff:'',reviewed_revision:2}),true);
});
