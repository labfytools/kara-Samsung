// Targeted browser lifecycle/clock checks, without audio hardware.
'use strict';
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const html=fs.readFileSync('bin/_kara_micro_test.html','utf8');
const script=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
function harness(media=()=>Promise.resolve(makeStream())){
 const nodes=new Map();
 const node=()=>({textContent:'',value:'90',disabled:false,children:[],appendChild(x){this.children.push(x);},replaceChildren(){this.children=[];}});
 const sandbox={console,URLSearchParams,ArrayBuffer,DataView,Float32Array,Int16Array,Uint8Array,Set,Promise,Math,Number,JSON,
  setTimeout,clearTimeout,setInterval,clearInterval,performance:{now:()=>0},
  location:{search:'?token=test',host:'localhost'},
  document:{visibilityState:'visible',getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},createElement:node,addEventListener(){}},
  navigator:{mediaDevices:{getUserMedia:media}},window:{isSecureContext:true,addEventListener(){}},
  WebSocket:class {static OPEN=1;},
  KaraCalibration:{levels(samples){return {rms:0,peak:0,clipped:0};}}};
 sandbox.window.KaraCalibration=sandbox.KaraCalibration;
 vm.createContext(sandbox);vm.runInContext(script,sandbox);
 return {sandbox,nodes,run:code=>vm.runInContext(code,sandbox)};
}
function makeStream(){const track={readyState:'live',enabled:true,muted:false,stops:0,stop(){this.stops++;},getSettings(){return {sampleRate:48000,deviceId:'PRIVATE_DEVICE',groupId:'PRIVATE_GROUP',echoCancellation:false,noiseSuppression:false,autoGainControl:false};}};return {track,getTracks:()=>[track],getAudioTracks:()=>[track]};}
async function main(){
 // A packet ending inside a render quantum must use its own boundary.
 let Processor;
 const audio={sampleRate:48000,currentFrame:0,currentTime:0,
  AudioWorkletProcessor:class {constructor(){this.port={postMessage:data=>this.messages.push(data)};this.messages=[];}},
  registerProcessor:(name,value)=>Processor=value};
 vm.createContext(audio);vm.runInContext(fs.readFileSync('bin/_kara_micro_worklet.js','utf8'),audio);
 const worklet=new Processor();
 for(let i=0;i<16;i++){audio.currentFrame=i*128;audio.currentTime=i*128/48000;worklet.process([[new Float32Array(128)]],[]);}
 assert.equal(worklet.messages[0].endFrame,960);assert.equal(worklet.messages[0].endSec,.02);
 assert.equal(worklet.messages[1].endFrame,1920);assert.equal(worklet.messages[1].sequence,1);
 audio.currentFrame=2304;worklet.process([[new Float32Array(128)]],[]);
 for(let i=1;i<=7;i++){audio.currentFrame=2304+i*128;worklet.process([[new Float32Array(128)]],[]);}
 assert.equal(worklet.messages[2].endFrame,3264,'partial packet dropped at input gap');
 const h=harness();
 h.run(`calibrationOwner={cancelled:false,context:{sampleRate:48000},packets:[],frames:0,lastEnd:null};`);
 for(let i=0;i<300;i++)h.run(`calibrationPacket(calibrationOwner,{pcm:new Int16Array(960).buffer,endFrame:${(i+1)*960},sequence:${i}})`);
 assert.equal(h.run('calibrationOwner.frames'),240000,'five-second memory limit');
 assert.equal(h.run('calibrationOwner.packets.length'),250);
 assert.equal(h.run('calibrationRecording(calibrationOwner,48000,50000).origin'),48000);
 // Wall-clock delivery time cannot shift a packet's frame index.
 h.sandbox.performance.now=()=>999999;
 assert.equal(h.run('calibrationRecording(calibrationOwner,48000,50000).origin'),48000);
 h.run('calibrationPacket(calibrationOwner,{pcm:new Int16Array(960).buffer,endFrame:290880,sequence:301})');
 assert.throws(()=>h.run('calibrationRecording(calibrationOwner,48000,50000)'),/Perte ou rupture/);
 // Cancel while getUserMedia is pending: a late track must stop, no new owner touched.
 let resolveMedia;const pending=new Promise(resolve=>resolveMedia=resolve);
 const c=harness(()=>pending);const running=c.run('startCalibration()');
 await c.run('stop()');
 const late=makeStream();resolveMedia(late);await running;
 assert.equal(late.track.stops,1);assert.equal(c.run('calibrationOwner'),null);
 assert.equal(c.nodes.get('calibrate').disabled,false);
 // Setup cancellation after context creation closes context and track exactly once.
 let finishModule;const modulePromise=new Promise(resolve=>finishModule=resolve);
 const stream=makeStream(),d=harness(()=>Promise.resolve(stream));let closes=0;
 d.sandbox.AudioContext=class {constructor(){this.sampleRate=48000;this.state='running';this.audioWorklet={addModule:()=>modulePromise};}resume(){return Promise.resolve();}close(){closes++;return Promise.resolve();}};
 const loading=d.run('startCalibration()');await new Promise(resolve=>setImmediate(resolve));
 await d.run('stop()');finishModule();await loading;
 assert.equal(stream.track.stops,1);assert.equal(closes,1);
 assert.equal(d.run('calibrationOwner'),null);
 // Reference wire format is strictly 4800 LE int16 samples; reject oversized data.
 const e=harness();
 const wire={rate:48000,channels:1,frames:4800,format:'s16le'};
 e.sandbox.wire=wire;
 e.run(`calibrationOwner={cancelled:false,pending:{method:'chirp',sent:0},reference:null};`);
 e.sandbox.payload=new ArrayBuffer(9600);new DataView(e.sandbox.payload).setInt16(0,-32768,true);
 e.run('calibrationMessage(calibrationOwner,{data:payload})');
 assert.equal(e.run('calibrationOwner.reference[0]'),-1);
 e.run(`calibrationMessage(calibrationOwner,{data:JSON.stringify({...wire,probe:'chirp',elapsed_ms:123})})`);
 assert.equal(e.run('calibrationOwner.pending.response.reference.length'),4800);
 e.run(`calibrationOwner.reference=null;calibrationMessage(calibrationOwner,{data:new ArrayBuffer(9602)})`);
 assert.match(e.run('calibrationOwner.failure'),/Référence serveur invalide/);
 e.run('calibrationOwner.failure=null;calibrationMessage(calibrationOwner,{data:payload})');
 e.run(`calibrationMessage(calibrationOwner,{data:JSON.stringify({...wire,probe:'chirp',rate:44100})})`);
 assert.match(e.run('calibrationOwner.failure'),/Format de référence serveur inattendu/);
 // Full campaign with a virtual audio clock and server, no real sleeps.
 const campaignStream=makeStream(),f=harness(()=>Promise.resolve(campaignStream));
 let now=0,ctx,packetNode;const commands=[],analyses=[];let summaryTrials;
 f.sandbox.performance.now=()=>now;
 f.sandbox.AudioContext=class {
  constructor(){ctx=this;this.sampleRate=48000;this.currentTime=0;this.state='running';this.audioWorklet={addModule:()=>Promise.resolve()};}
  resume(){return Promise.resolve();}close(){this.state='closed';return Promise.resolve();}
  createMediaStreamSource(){return {connect(){},disconnect(){}};}
  createGain(){return {gain:{value:1},connect(){},disconnect(){}};}
 };
 f.sandbox.AudioWorkletNode=class {
  constructor(){packetNode=this;this.port={postMessage:data=>assert.equal(data.gain,1),onmessage:null};}
  connect(){}disconnect(){}
 };
 f.sandbox.WebSocket=class {
  static OPEN=1;
  constructor(url){assert.match(url,/calibration=1/);this.readyState=1;queueMicrotask(()=>this.onmessage({data:JSON.stringify({ready:true,device:'bluez_output.AA_BB_CC_DD_EE_FF',target:'PRIVATE_TARGET'})}));}
  send(value){
   assert.equal(typeof value,'string','never transmit microphone PCM');commands.push({value,at:now});
   if(value.startsWith('ping:')){assert.match(value,/^ping:\d+\.\d{3}$/);this.onmessage({data:'pong:'+value.slice(5)});}
   if(value.startsWith('probe:')){
    this.onmessage({data:new ArrayBuffer(9600)});
    this.onmessage({data:JSON.stringify({...wire,probe:value.slice(6),request_to_write_ms:123,target:'PRIVATE_TARGET'})});
   }
  }
  close(){this.readyState=3;}
 };
 f.sandbox.KaraCalibration={
  levels:()=>({rms:.2,peak:.5,clipped:0}),
  resample:(samples,from,to)=>{assert.equal(from,48000);assert.equal(to,48000);return samples;},
  analyze:(recording,reference,rate,anchor,noise)=>{
   assert.ok(recording.length<=rate*1.6);assert.equal(reference.length,4800);assert.equal(noise,.2);
   assert.ok(anchor>=rate*.4&&anchor<rate*.42);analyses.push(anchor);
   return {ok:true,reason:'Arrivée isolée',delayMs:100,correlation:.9,confidence:.8,rms:.2,peak:.5,clipped:0,gainDb:0,snrDb:20,reflections:[],polarity:1};
  },
  summarize:trials=>{summaryTrials=trials;return {status:'reliable',best:{method:'chirp',medianMs:100,madMs:0,inliers:6},methods:[],trials};}
 };
 f.sandbox.window.KaraCalibration=f.sandbox.KaraCalibration;
 f.sandbox.advance=ms=>{
  now+=ms;
  const last=Math.floor(now/20)*960;
  while((ctx.frames||0)<last){
   ctx.frames=(ctx.frames||0)+960;ctx.currentTime=ctx.frames/48000;
   if(packetNode.port.onmessage)packetNode.port.onmessage({data:{pcm:new Int16Array(960).buffer,endFrame:ctx.frames,sequence:ctx.frames/960-1}});
  }
  ctx.currentTime=now/1000+.000007; // Preserve a fractional anchor sample.
 };
 f.run('calibrationWait=async(owner,ms)=>{calibrationActive(owner);advance(ms);};');
 await f.run('startCalibration()');
 assert.equal(analyses.length,12);assert.equal(summaryTrials.length,12);
 assert.ok(analyses.every(anchor=>!Number.isInteger(anchor)));
 const probes=commands.filter(c=>c.value.startsWith('probe:'));
 assert.equal(probes.length,13);assert.equal(probes[0].value,'probe:warmup');
 for(let i=1;i<13;i++){
  assert.equal(probes[i].value,'probe:'+(i%2?'chirp':'pn'));
  assert.ok(probes[i].at-probes[i-1].at>1000);
 }
 assert.equal(campaignStream.track.stops,1);assert.equal(ctx.state,'closed');
 assert.equal(f.run('calibrationOwner'),null);
 assert.match(f.nodes.get('calibration').textContent,/Mesure fiable.*chirp/);
 assert.match(f.nodes.get('calibration-level').textContent,/niveau élevé/);
 assert.match(f.nodes.get('calibration-meta').textContent,/campagne entière/);
 assert.ok(commands.some(c=>c.value==='stop'));
 const report=[...f.nodes.values()].map(node=>node.textContent).join(' ');
 assert.doesNotMatch(report,/PRIVATE_|AA_BB_CC_DD_EE_FF|deviceId|groupId/);
 assert.match(report,/Bluetooth \(identifiant masqué\)/);
 assert.match(report,/Worklet → JS p95/);
 assert.match(report,/request_to_write_ms/);
 assert.match(report,/Réduire le bruit ambiant/);
 const cells=f.nodes.get('calibration-table').children.flatMap(row=>row.children.map(cell=>cell.textContent));
 assert.ok(cells.includes('Arrivée isolée'));assert.ok(cells.includes('Bruit RMS'));
 assert.equal(summaryTrials[0].noiseRms,.2);
 console.log('OK: exact packet boundaries, gap detection, bounded capture, delayed delivery, setup cancellation, LE reference contract, full alternating campaign, level warning, cleanup');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
