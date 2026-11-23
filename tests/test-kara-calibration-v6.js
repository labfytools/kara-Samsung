#!/usr/bin/env node
// V6 regression: direct digital reference; no Android speaker capture loop.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../bin/_kara_micro_test.html'),'utf8');
const start=html.indexOf('function makeCalibrationTone(');
const end=html.indexOf('function sleep(ms)',start);
const calibration=html.slice(html.indexOf('async function runAutomaticCalibration(){'),html.indexOf("$('speaker-volume').oninput",start));
assert(start>0&&end>start);
const ctx=vm.createContext({Float32Array,Math});
vm.runInContext(html.slice(start,end),ctx);
const tone=ctx.makeCalibrationTone(48000);
assert(tone.length===Math.round(48000*.36));
const reference=Float32Array.from(tone,s=>s*.65);
assert(reference.length===tone.length);
assert(reference.every(x=>Math.abs(x)<=.14));
assert(calibration.includes('const reference=Float32Array.from(digitalTone,s=>s*0.65)'));
assert(calibration.includes('const sent=await transmitRecordedClip(reference,rate)'));
assert(calibration.includes('analyzeFirstArrival(captured,reference,rate,anchorMs)'));
assert(!calibration.includes('playCalibrationTone('),'V6 must not play a phone beep');
assert(!calibration.includes('findRecordedTone('),'V6 must not use an acoustic recording as reference');
assert(calibration.includes('for(let trial=0;trial<=5;trial++)'));
assert(calibration.includes('if(!warmup)results.push(result)'));
assert(html.includes('if(calibrationMode)return; // NEVER forward live microphone in calibration'));
assert(html.includes("ws.send('monitor:off');"));
const tx=html.slice(html.indexOf('async function transmitRecordedClip('),html.indexOf('function analyzeRecordedReturn('));
assert(tx.includes('bytes[i]=Math.round(Math.max(-1,Math.min(1,clip[offset+i]))*32767)'));
console.log('PASS: digital reference, bounded samples, no phone tone, five trials, anti-Larsen');
