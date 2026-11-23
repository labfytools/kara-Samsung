#!/usr/bin/env node
// Synthetic first-arrival regressions; calibrated V3 remains separately tested.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../bin/_kara_micro_test.html'),'utf8');
const start=html.indexOf('function correlateTone('),end=html.indexOf('async function runAutomaticCalibration()',start);
assert(start>0&&end>start);
const context=vm.createContext({Math,Float32Array,Int16Array,calibrationSamples:[],calibrationCaptureStartSec:null,calibrationCaptureFrames:0});
vm.runInContext(html.slice(start,end),context);
const rate=48000,clip=new Float32Array(rate/4);let seed=1234;
for(let i=0;i<clip.length;i++){seed=(Math.imul(seed,1664525)+1013904223)>>>0;clip[i]=(seed/4294967296-0.5)*0.5;}
function recording(events){const out=new Float32Array(rate*2);for(const [ms,gain] of events){const offset=Math.round(ms*rate/1000);for(let i=0;i<clip.length;i++)out[offset+i]+=clip[i]*gain;}return out;}
const single=context.analyzeFirstArrival(recording([[330,1]]),clip,rate,100);
assert(single.ok,JSON.stringify(single));assert(Math.abs(single.delay-230)<=3);
const echo=context.analyzeFirstArrival(recording([[330,.85],[550,1]]),clip,rate,100);
assert(echo.ok,JSON.stringify(echo));assert(Math.abs(echo.delay-230)<=3,JSON.stringify(echo));
assert(Math.abs(echo.dominantDelay-450)<=3,JSON.stringify(echo));assert(echo.echoes>=1,JSON.stringify(echo));
const tooWeak=context.analyzeFirstArrival(recording([[330,.16],[550,1]]),clip,rate,100);
assert(tooWeak.ok,JSON.stringify(tooWeak));assert(Math.abs(tooWeak.delay-450)<=3,JSON.stringify(tooWeak));
const silent=context.analyzeFirstArrival(recording([]),clip,rate,100);assert(!silent.ok);
assert(html.includes('for(let trial=0;trial<=5;trial++)'));
assert(html.includes('if(!warmup)results.push(result)'));
assert(html.includes('if(calibrationMode)return; // NEVER forward live microphone in calibration'));
assert(html.includes("ws.send('monitor:off');"));
console.log('PASS: V4 first arrival, stronger later echo, weak pre-echo rejection, silence, prime, safety');
