#!/usr/bin/env node
// Synthetic regression: distinct competing arrival must not pass as a unique delay.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../bin/_kara_micro_test.html'),'utf8');
const start=html.indexOf('function correlateTone(');
const end=html.indexOf('async function runAutomaticCalibration()',start);
assert(start>=0 && end>start);
const ctx=vm.createContext({Math,Float32Array,Int16Array,calibrationSamples:[],calibrationCaptureStartSec:null,calibrationCaptureFrames:0});
vm.runInContext(html.slice(start,end),ctx);
const rate=48000, clip=new Float32Array(rate/4);
let seed=1;
for(let i=0;i<clip.length;i++){
 seed=(Math.imul(seed,1664525)+1013904223)>>>0;
 clip[i]=((seed/4294967296)-0.5)*0.5;
}
function recording(events){
 const out=new Float32Array(rate*2);
 for(const [timeMs,gain] of events){
  const offset=Math.round(timeMs*rate/1000);
  for(let i=0;i<clip.length;i++)out[offset+i]+=gain*clip[i];
 }
 return out;
}
const clean=ctx.analyzeRecordedReturn(recording([[300,1]]),clip,rate,100);
assert(clean.ok,JSON.stringify(clean));
assert(Math.abs(clean.delay-200)<=3);
const ambiguous=ctx.analyzeRecordedReturn(recording([[300,1],[650,0.98]]),clip,rate,100);
assert(!ambiguous.ok,JSON.stringify(ambiguous));
assert.equal(ambiguous.reason,'pic acoustique ambigu');
const silence=ctx.analyzeRecordedReturn(recording([]),clip,rate,100);
assert(!silence.ok);
assert.equal(silence.reason,'corrélation insuffisante');
assert(html.includes('for(let trial=0;trial<=5;trial++)'),'V3 runs one prime plus five measurements');
assert(html.includes('if(!warmup)results.push(result)'),'prime must be excluded');
assert(html.includes('if(calibrationMode)return; // NEVER forward live microphone in calibration'));
assert(html.includes("ws.send('monitor:off');"));
assert(html.includes("ws.send('monitor:on');"));
console.log('PASS: single arrival, ambiguous echo rejection, silence, prime exclusion, no live-mic forwarding');
