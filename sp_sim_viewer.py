# -*- coding: utf-8 -*-
from __future__ import annotations


def build_simulation_html(
    image_base64: str,
    image_width: int,
    image_height: int,
    graph_json: str,
    positions_json: str,
) -> str:
    return (_TEMPLATE
        .replace("__IMAGE_B64__", image_base64)
        .replace("__IMG_W__", str(image_width))
        .replace("__IMG_H__", str(image_height))
        .replace("__GRAPH_JSON__", graph_json)
        .replace("__POSITIONS_JSON__", positions_json))


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{font-family:'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',sans-serif;background:#0d0d1a}
#sim-container{width:100%;height:100%;display:flex;flex-direction:column;position:relative}
#svg-wrap{flex:1;display:flex;justify-content:center;align-items:center;background:#0d0d1a;border:1px solid rgba(99,102,241,.2);border-radius:12px;margin:0 0 8px 0;padding:10px;overflow:hidden;min-height:0}
#main-svg{display:block;max-width:100%;max-height:100%;width:auto;height:auto}
@keyframes botPulse{0%,100%{opacity:.25}50%{opacity:.7}}
.bot-aura{animation:botPulse 1.8s ease-in-out infinite}
.bot-group{pointer-events:none}
@keyframes flowDash{to{stroke-dashoffset:-24}}
.edge-flow{fill:none;stroke:rgba(99,102,241,.5);stroke-width:2.5;stroke-dasharray:4 20;stroke-linecap:round;animation:flowDash 1.2s linear infinite;opacity:0}
.edge-flow.active{opacity:1}
#status-bar{padding:8px 14px;background:rgba(13,13,26,.95);border:1px solid rgba(99,102,241,.25);border-radius:10px;height:40px;display:flex;align-items:center;justify-content:center}
#status-text{font-size:13px;color:#818cf8;font-weight:700;letter-spacing:.3px}
#summary-panel{position:absolute;bottom:60px;right:16px;background:rgba(13,13,26,.92);backdrop-filter:blur(12px);border:1px solid rgba(99,102,241,.3);border-radius:12px;padding:16px 20px;box-shadow:0 8px 32px rgba(99,102,241,.15);min-width:220px;pointer-events:auto;color:#e2e8f0}
#summary-panel h3{font-size:14px;color:#c7d2fe;margin-bottom:10px}
.sr{display:flex;justify-content:space-between;padding:3px 0;font-size:13px}
.sr .l{color:#94a3b8}.sr .v{font-weight:800}
.sr .pass{color:#4ade80}.sr .fail{color:#f87171}.sr .hold{color:#fbbf24}
</style>
</head>
<body>
<div id="sim-container">
  <div id="svg-wrap">
    <svg id="main-svg" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
         viewBox="0 0 __IMG_W__ __IMG_H__">
      <defs>
        <filter id="glow-bot" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="10" result="b"/>
          <feFlood flood-opacity="0.3"/><feComposite in2="b" operator="in"/>
          <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <filter id="box-glow" x="-5%" y="-5%" width="110%" height="110%">
          <feGaussianBlur stdDeviation="4" result="b"/>
          <feFlood flood-color="#818cf8" flood-opacity="0.4"/>
          <feComposite in2="b" operator="in"/>
          <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <filter id="ind-shadow" x="-5%" y="-5%" width="110%" height="120%">
          <feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="rgba(0,0,0,0.5)"/>
        </filter>
        <linearGradient id="g-ocr" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#818cf8"/><stop offset="100%" stop-color="#4f46e5"/>
        </linearGradient>
        <linearGradient id="g-judge" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#34d399"/><stop offset="100%" stop-color="#059669"/>
        </linearGradient>
        <linearGradient id="g-permit" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#c084fc"/><stop offset="100%" stop-color="#7c3aed"/>
        </linearGradient>
      </defs>
      <image href="data:image/png;base64,__IMAGE_B64__" width="__IMG_W__" height="__IMG_H__" x="0" y="0"/>
      <g id="flow-layer"></g>
      <g id="box-layer"></g>
      <g id="ind-layer"></g>
      <g id="fx-layer"></g>
      <g id="bot-layer"></g>
    </svg>
  </div>
  <div id="status-bar"><span id="status-text">분석 중...</span></div>
</div>
<script>
(function(){
const DATA=__GRAPH_JSON__,POS=__POSITIONS_JSON__;
const C_P='#4ade80',C_F='#f87171',C_H='#fbbf24';
const MOVE=0.5,SCAN=0.7,EVAL=0.7,STEP=1.5,REV=0.6;
const IND_W=220,IND_H=44,IND_GAP=8;

const nodesById={};
DATA.nodes.forEach(n=>{nodesById[n.id]=n});
DATA.nodes.forEach(n=>{
  const p=POS[n.label];
  if(p){n._x0=p[0];n._y0=p[1];n._x1=p[2];n._y1=p[3];
    n._cx=(p[0]+p[2])/2;n._cy=(p[1]+p[3])/2;n._w=p[2]-p[0];n._h=p[3]-p[1];}
});
const placed=DATA.nodes.filter(n=>n._cx!==undefined);
if(!placed.length)return;
const topo=[];
DATA.layers.forEach(l=>{l.forEach(nid=>{if(nodesById[nid]._cx!==undefined)topo.push(nid)})});

function svgEl(t,a){const e=document.createElementNS('http://www.w3.org/2000/svg',t);for(const[k,v]of Object.entries(a||{}))e.setAttribute(k,v);return e}

const flowL=document.getElementById('flow-layer');
const boxL=document.getElementById('box-layer');
const indL=document.getElementById('ind-layer');
const fxL=document.getElementById('fx-layer');
const botL=document.getElementById('bot-layer');

// ── edge flows ──
DATA.edges.forEach((e,i)=>{
  const s=nodesById[e.source],t=nodesById[e.target];
  if(!s._cx||!t._cx)return;
  const sx=s._cx,sy=s._y1+2,tx=t._cx,ty=t._y0-2,gap=ty-sy;
  flowL.appendChild(svgEl('path',{'class':'edge-flow',id:`fl-${i}`,
    d:`M${sx} ${sy}C${sx} ${sy+gap*.4},${tx} ${ty-gap*.4},${tx} ${ty}`}));
});

// ── OCR highlight boxes (initially hidden) ──
const boxes={};
placed.forEach(n=>{
  const r=svgEl('rect',{x:n._x0-4,y:n._y0-4,width:n._w+8,height:n._h+8,rx:6,
    fill:'none',stroke:'#818cf8','stroke-width':3,filter:'url(#box-glow)'});
  r.style.opacity=0;
  boxL.appendChild(r);
  boxes[n.id]=r;
});

// ── indicator panels (shown by 판정봇) ──
const indEls={};
placed.forEach(n=>{
  const ix=n._x0-4, iy=n._y1+IND_GAP;
  const g=svgEl('g',{id:`ind-${n.id}`,transform:`translate(${ix},${iy})`});
  g.style.opacity=0;
  g.appendChild(svgEl('rect',{width:IND_W,height:IND_H,rx:8,
    fill:'rgba(13,13,26,0.88)',stroke:'rgba(99,102,241,0.4)','stroke-width':1.5,
    filter:'url(#ind-shadow)'}));

  const secs=[
    {key:'pass',color:C_P,target:n.pass_count,label:'합격',ox:10},
    {key:'fail',color:C_F,target:n.fail_count,label:'불합격',ox:80},
    {key:'hold',color:C_H,target:n.hold_count,label:'보류',ox:154},
  ];
  const els={};
  secs.forEach(s=>{
    g.appendChild(svgEl('circle',{cx:s.ox+7,cy:IND_H/2-2,r:6,fill:'#334155'}));
    const dot=g.lastChild;
    const num=svgEl('text',{x:s.ox+20,y:IND_H/2-2,fill:'#475569',
      'font-size':'18px','font-weight':'900','dominant-baseline':'central'});
    num.textContent='–';g.appendChild(num);
    const lbl=svgEl('text',{x:s.ox+40,y:IND_H/2-2,fill:'rgba(148,163,184,0.8)',
      'font-size':'12px','font-weight':'600','dominant-baseline':'central'});
    lbl.textContent=s.label;g.appendChild(lbl);
    els[s.key]={dot,num,color:s.color,target:s.target};
  });
  indL.appendChild(g);
  indEls[n.id]={g,els};
});

function countUp(nid){
  const info=indEls[nid];
  ['pass','fail','hold'].forEach(k=>{
    const s=info.els[k];
    gsap.to(s.dot,{attr:{fill:s.color},duration:.3});
    s.num.setAttribute('fill',s.color);
    if(s.target===0){s.num.textContent='0';return}
    const p={v:0};
    gsap.to(p,{v:s.target,duration:.6,ease:'power1.out',
      onUpdate:()=>{s.num.textContent=Math.round(p.v)}});
  });
}
function resetInd(nid){
  indEls[nid].g.style.opacity=0;
  ['pass','fail','hold'].forEach(k=>{
    const s=indEls[nid].els[k];
    s.num.textContent='–';s.num.setAttribute('fill','#475569');
    s.dot.setAttribute('fill','#334155');
  });
}

// ── verdict badges ──
const bdgs={};
placed.forEach(n=>{
  const b=svgEl('text',{x:n._x1+20,y:n._cy,id:`bdg-${n.id}`,
    'font-size':'28px','font-weight':'900','text-anchor':'middle','dominant-baseline':'central'});
  b.style.opacity=0;fxL.appendChild(b);bdgs[n.id]=b;
});

// ── AI bots ──
function createBot(id,gradId,accent,label){
  const g=svgEl('g',{'class':'bot-group',id});g.style.opacity=0;
  g.appendChild(svgEl('circle',{cx:0,cy:0,r:44,fill:'none',stroke:accent,'stroke-width':2,opacity:.2,'class':'bot-aura'}));
  g.appendChild(svgEl('rect',{x:-28,y:-32,width:56,height:52,rx:14,fill:`url(#${gradId})`,stroke:'rgba(255,255,255,0.2)','stroke-width':2,filter:'url(#glow-bot)'}));
  g.appendChild(svgEl('line',{x1:0,y1:-32,x2:0,y2:-44,stroke:'rgba(255,255,255,0.5)','stroke-width':2,'stroke-linecap':'round'}));
  g.appendChild(svgEl('circle',{cx:0,cy:-46,r:5,fill:accent}));
  g.appendChild(svgEl('circle',{cx:-10,cy:-14,r:5,fill:'rgba(255,255,255,0.9)'}));
  g.appendChild(svgEl('circle',{cx:10,cy:-14,r:5,fill:'rgba(255,255,255,0.9)'}));
  g.appendChild(svgEl('circle',{cx:-8,cy:-14,r:2.5,fill:'#0f172a'}));
  g.appendChild(svgEl('circle',{cx:12,cy:-14,r:2.5,fill:'#0f172a'}));
  g.appendChild(svgEl('path',{d:'M-8 2Q0 10 8 2',fill:'none',stroke:'rgba(255,255,255,0.6)','stroke-width':2,'stroke-linecap':'round'}));
  const nm=svgEl('text',{x:0,y:34,fill:accent,'font-size':'14px','font-weight':'800','text-anchor':'middle','dominant-baseline':'hanging','letter-spacing':'0.5px'});
  nm.textContent=label;g.appendChild(nm);
  botL.appendChild(g);return g;
}
const ocrBot=createBot('ocr-bot','g-ocr','#818cf8','OCR봇');
const judgeBot=createBot('judge-bot','g-judge','#34d399','판정봇');
const permitBot=createBot('permit-bot','g-permit','#c084fc','허가서봇');

function verd(n){if(n.fail_count>0)return'fail';if(n.hold_count>0)return'hold';return'pass'}

// ── timeline (순차: OCR 전부 → 판정봇 전부 → 허가서봇) ──
const tl=gsap.timeline({paused:true});
const st=document.getElementById('status-text');
const OCR_STEP=1.0, JUDGE_STEP=1.2;
let t=0;

// ═══ Phase 1: OCR봇 — 모든 노드를 순서대로 스캔, 박스 표시 ═══
const fn=nodesById[topo[0]];
tl.set(ocrBot,{x:fn._cx,y:fn._y0-60,opacity:0});
tl.to(ocrBot,{opacity:1,duration:.4,ease:'power2.out'});
t=.4;

topo.forEach((nid,i)=>{
  const n=nodesById[nid],box=boxes[nid];

  tl.to(ocrBot,{x:n._cx,y:n._y0-60,duration:MOVE,ease:'power2.inOut',
    onStart:()=>{st.textContent=`OCR 스캔: ${n.label}`}},t);

  DATA.edges.forEach((e,ei)=>{
    if(e.target===nid)tl.call(()=>{const f=document.getElementById(`fl-${ei}`);if(f)f.classList.add('active')},[], t);
  });

  tl.to(box,{opacity:1,duration:.3,ease:'power2.out'},t+MOVE);
  tl.fromTo(box,{'stroke-width':3},{attr:{'stroke-width':5},duration:.2,yoyo:true,repeat:1},t+MOVE+.1);

  t+=OCR_STEP;
});

// OCR봇 퇴장
tl.to(ocrBot,{opacity:0,duration:.3},t);
t+=.6;

// ═══ Phase 2: 판정봇 — 모든 노드를 순서대로 판정, 인디케이터 + 배지 표시 ═══
const jn=nodesById[topo[0]];
tl.set(judgeBot,{x:jn._cx,y:jn._y0-60,opacity:0},t);
tl.to(judgeBot,{opacity:1,duration:.4,ease:'power2.out'},t);
t+=.4;

topo.forEach((nid,i)=>{
  const n=nodesById[nid],ind=indEls[nid],bdg=bdgs[nid],v=verd(n);

  tl.to(judgeBot,{x:n._cx,y:n._y0-60,duration:MOVE,ease:'power2.inOut',
    onStart:()=>{st.textContent=`판정 중: ${n.label}`}},t);

  tl.to(ind.g,{opacity:1,duration:.25},t+MOVE+.1);
  tl.call(()=>{countUp(nid)},[], t+MOVE+.15);

  tl.call(()=>{
    bdg.textContent=v==='pass'?'✓':v==='fail'?'✗':'!';
    bdg.setAttribute('fill',v==='pass'?C_P:v==='fail'?C_F:C_H);
  },[], t+MOVE+EVAL*.7);
  tl.fromTo(bdg,{opacity:0,scale:0},{opacity:1,scale:1,duration:.3,ease:'back.out(2)',transformOrigin:'center center'},t+MOVE+EVAL*.7);

  t+=JUDGE_STEP;
});

// 판정봇 퇴장
tl.to(judgeBot,{opacity:0,duration:.3},t);
t+=.6;

// ═══ Phase 3: 허가서봇 — 보류 항목만 ═══
const hn=topo.filter(nid=>nodesById[nid].hold_count>0);
if(hn.length>0){
  const fh=nodesById[hn[0]];
  tl.set(permitBot,{x:fh._cx,y:fh._y0-60,opacity:0},t);
  tl.to(permitBot,{opacity:1,duration:.3},t);
  tl.call(()=>{st.textContent='허가서봇: 보류 항목 검토 중...'},[], t);
  t+=.3;
  hn.forEach((nid,i)=>{
    const n=nodesById[nid];
    tl.to(permitBot,{x:n._cx,y:n._y0-60,duration:REV*.5,ease:'power1.inOut'},t);
    tl.call(()=>{st.textContent=`보류 검토: ${n.label} (${n.hold_count}건)`},[], t);
    t+=REV;
  });
  tl.to(permitBot,{opacity:0,duration:.3},t);
  t+=.5;
}

// ── summary ──
const so=t+.2;
const tp_=DATA.nodes.reduce((s,n)=>s+n.pass_count,0);
const tf_=DATA.nodes.reduce((s,n)=>s+n.fail_count,0);
const th_=DATA.nodes.reduce((s,n)=>s+n.hold_count,0);
const ta_=DATA.nodes.reduce((s,n)=>s+n.total_count,0);
const sd=document.createElement('div');sd.id='summary-panel';sd.style.opacity='0';
sd.innerHTML=`<h3>📋 전체 판정 요약</h3>
<div class="sr"><span class="l">전체 시험</span><span class="v">${ta_}건</span></div>
<div class="sr"><span class="l">합격</span><span class="v pass">${tp_}건</span></div>
<div class="sr"><span class="l">불합격</span><span class="v fail">${tf_}건</span></div>
${th_>0?`<div class="sr"><span class="l">보류</span><span class="v hold">${th_}건</span></div>`:''}
<div class="sr" style="border-top:1px solid rgba(99,102,241,.2);padding-top:6px;margin-top:4px">
<span class="l">합격률</span><span class="v" style="color:${tf_>0?'#f87171':'#4ade80'}">${ta_>0?Math.round(tp_/ta_*100):0}%</span></div>`;
document.getElementById('sim-container').appendChild(sd);
tl.to(sd,{opacity:1,duration:.5,ease:'power2.out'},so+.2);
tl.call(()=>{st.textContent=hn.length>0?'판정 완료 (보류 있음)':'판정 완료'},[], so+.3);

// ── auto-play ──
tl.play();
})();
</script>
</body>
</html>
"""
