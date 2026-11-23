#!/usr/bin/env node
// A nonrepeating calibration reference must have a sharp autocorrelation and
// distinguish direct paths from a stronger delayed echo.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../bin/_kara_micro_test.html'),'utf8');
const toneStart=html.indexOf('function makeCalibrationTone(');
const toneEnd=html.indexOf('function sleep(ms)',toneStart);
const start=html.indexOf('function correlateTone(');
const end=html.indexOf('async function runAutomaticCalibration()',start);
assert(toneStart>0&&toneEnd>toneStart&&start>0&&end>start);
const ctx=vm.createContext({Math,Float32Array,Int16Array,calibrationSamples:[],calibrationCaptureStartSec:null,calibrationCaptureFrames:0});
vm.runInContext(html.slice(toneStart,toneEnd)+html.slice(start,end),ctx);
const rate=48000,tone=ctx.makeCalibrationTone(rate);
const clip=tone.slice(0,rate/4);
function corr(shift){let dot=0,e0=0,e1=0;for(let i=0;i<clip.length-shift;i++){const a=clip[i],b=clip[i+shift];dot+=a*b;e0+=a*a;e1+=b*b;}return dot/Math.sqrt(e0*e1);}
assert(Math.abs(corr(Math.round(.12*rate)))<.30,'V5 reference has a 120ms alias');
assert(Math.max(...clip)<.25&&Math.min(...clip)>-.25,'reference should be volume limited');
for(let i=1;i<clip.length;i++)assert(Math.abs(clip[i]-clip[i-1])<.20,'reference should not click at chip boundary');
function recording(events){const out=new Float32Array(rate*2);for(const [ms,gain] of events){const offset=Math.round(ms*rate/1000);for(let i=0;i<clip.length;i++)out[offset+i]+=clip[i]*gain;}return out;}
const single=ctx.analyzeFirstArrival(recording([[330,1]]),clip,rate,100);
assert(single.ok&&Math.abs(single.delay-230)<=3,JSON.stringify(single));
const echo=ctx.analyzeFirstArrival(recording([[330,.85],[450,1]]),clip,rate,100);
assert(echo.ok&&Math.abs(echo.delay-230)<=3,JSON.stringify(echo));
assert(Math.abs(echo.dominantDelay-350)<=3,JSON.stringify(echo));
assert(!ctx.analyzeFirstArrival(recording([]),clip,rate,100).ok,'silence must be rejected');
assert(html.includes('for(let trial=0;trial<=5;trial++)'));
assert(html.includes('if(!warmup)results.push(result)'));
assert(html.includes('if(calibrationMode)return; // NEVER forward live microphone in calibration'));
assert(html.includes("ws.send('monitor:off');"));
console.log('PASS: V5 unique reference, quiet onset, direct/120ms echo, silence, anti-live-mic');
