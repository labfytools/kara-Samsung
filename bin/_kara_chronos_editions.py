"""Audio-only edition comparison on independently hashed recordings."""
import subprocess
import numpy as np
from _kara_turbo import digest as sha

def features(p):
    duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(p)],timeout=15))
    if not .1<duration<600:raise ValueError('duration')
    raw=subprocess.check_output(['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-threads','1','-i',str(p),'-t','600','-ac','1','-ar','8000','-f','f32le','-'],timeout=60)
    sig=np.frombuffer(raw,dtype='<f4')
    if len(sig)<1024:return np.empty((0,1),dtype='f'),duration
    frames=np.lib.stride_tricks.sliding_window_view(sig,1024)[::400]
    spectrum=np.abs(np.fft.rfft(frames*np.hanning(1024),axis=-1))
    bins=np.unique(np.round(np.geomspace(6,505,50)).astype(int))
    f=np.stack([np.log(.001+spectrum[:,a:b].mean(axis=1)) for a,b in zip(bins,bins[1:])],axis=1)
    f-=f.mean(0)
    f-=f.mean(1,keepdims=True)
    return (f/np.maximum(np.linalg.norm(f,axis=1,keepdims=True),1e-6)).astype('f'),duration

def compare(a,b):
    x,dx=features(a);y,dy=features(b)
    # Find distinctive short acoustic landmarks, then fit one clock map.
    # Repeated motifs can be genuine secondary peaks; leave them ambiguous.
    matches=[]
    for k in (range(40,len(x)-40,60) if len(y)>50 else ()):
        ix=np.arange(-24,25,4)
        scores=sum(y[24+j:len(y)-24+j]@x[k+j] for j in ix)/len(ix)
        j=int(scores.argmax())
        rest=scores.copy();rest[max(0,j-30):j+31]=-2
        matches.append([k*.05,(j+24)*.05,float(scores[j]),float(scores[j]-rest.max())])
    anchors=[(s,t) for s,t,v,m in matches if v>=.60 and m>=.06]
    best=[]
    for i,(s,t) in enumerate(anchors):
        for u,v in anchors[i+1:]:
            rate=(v-t)/(u-s)
            if .75<=rate<=1.3:
                group=[(k,j) for k,j in anchors if abs(j-(t+(k-s)*rate))<=.25]
                if len(group)>len(best):best=group
    result=dict(source_sha=sha(a),target_sha=sha(b),source_seconds=dx,target_seconds=dy,windows=len(matches),
        distinctive=len(anchors),inliers=len(best),mapping=None,anchors=matches)
    if len(best)>=4 and best[-1][0]-best[0][0]>=15:
        rate,offset=np.polyfit(*zip(*best),1)
        result['mapping']=dict(target_seconds_per_source_second=rate,offset=offset,source_verified_centers=[best[0][0],best[-1][0]],target_verified_centers=[best[0][1],best[-1][1]])
    return result
