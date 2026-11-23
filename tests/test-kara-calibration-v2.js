#!/usr/bin/env node
// Regression test: calibrated acoustic correlation uses the AudioContext clock
// rather than the number of captured frames since a JS array reset.
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const html=fs.readFileSync(new URL('../bin/_kara_micro_test.html',`file://${__filename}`),'utf8');
const start=html.indexOf('function correlateTone(');
const end=html.indexOf('async function runAutomaticCalibration()',start);
assert(start>0 && end>start,'Calibration helpers must exist');
const ctx=vm.createContext({Float32Array,Int16Array,Math,calibrationSamples:[],calibrationCaptureStartSec:null,calibrationCaptureFrames:0});
vm.runInContext(html.slice(start,end),ctx);
const rate=48000;
const clip=new Float32Array(Math.round(rate*.25));
let seed=123456789;
for(let i=0;i<clip.length;i++){seed=(Math.imul(seed,1664525)+1013904223)>>>0;clip[i]=(seed/2**32-.5)*.4;}
for(const [anchor,delay] of [[160,250],[440,300],[240,540]]){
 const recording=new Float32Array(rate*2);
 const offset=Math.round((anchor+delay)*rate/1000);
 recording.set(clip,offset);
 const got=ctx.analyzeRecordedReturn(recording,clip,rate,anchor);
 assert(got.ok,JSON.stringify(got));
 assert(Math.abs(got.delay-delay)<=3,`${anchor}+${delay}: observed ${got.delay}`);
}
const noTone=ctx.analyzeRecordedReturn(new Float32Array(rate*2),clip,rate,440);
assert.equal(noTone.ok,false,'Silence cannot be a valid calibration');
assert.equal(noTone.reason,'corrélation insuffisante');
// Verify the browser sends only pre-recorded clip in calibration mode.
assert(html.includes('if(calibrationMode)return; // NEVER forward live microphone in calibration'));
assert(html.includes("ws.send('monitor:off');"));
console.log('PASS: anchor clocks, delays, silence rejection, anti-live-mic guard');
