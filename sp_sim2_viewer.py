# -*- coding: utf-8 -*-
"""2페이지 검수 시뮬레이션 뷰어 (n8n 스타일).

레이아웃
  ┌──────────────────────────────────────────┐
  │ A (상단 70%) : 제조 flowchart              │  ← 문서의 위→아래 흐름을
  │   - 반시계 90° 회전 → 왼쪽→오른쪽 흐름      │     반시계 90° 돌려 가로 배치
  │   - OCR봇이 '이름만' 박스를 작도            │
  │   - 판정봇이 분수형 인디케이터로 판정        │
  ├──────────────────────────────────────────┤
  │ B (하단 30%) : 고정 n8n 파이프라인          │
  │   Start ─ OCR ─ 판정 ─ End                  │
  │                 │                           │
  │       ┌─────────┴──────────┐                │
  │   날짜선행관계        시험기준판별             │
  │                    │   │   │                 │
  │              기호 사전 허가서 생물학적제제 기준 │  ← 파트별 RAG 소스
  └──────────────────────────────────────────┘

판정 순서(노드마다)
  1) 규칙 기반 1차 판정
  2) 참고 근거 기반 판정: 공정일자 정합성 검증과 시험 기준/결과 의미 비교

봇
  - OCR봇   : A에서 박스 작도 후 퇴장
  - 판정봇 : 날짜, 기호 사전, 허가서, 생물학적제제 기준 및 시험방법 근거를 오가며 판정

세부(실제 CSV 연동, 판정 근거 텍스트, RAG 매칭)는 추후 확장.
"""
from __future__ import annotations

import json


def _safe_graph_json(graph_json: str) -> str:
    """HTML에 직접 삽입해도 깨지지 않는 그래프 JSON으로 정규화한다."""
    fallback = {"product_name": "", "nodes": [], "edges": [], "layers": []}
    try:
        data = json.loads(graph_json or "")
        if not isinstance(data, dict):
            data = fallback
    except Exception:
        data = fallback

    if not isinstance(data.get("nodes"), list):
        data["nodes"] = []
    if not isinstance(data.get("edges"), list):
        data["edges"] = []
    if not isinstance(data.get("layers"), list):
        data["layers"] = []
    return json.dumps(data, ensure_ascii=False)


def build_simulation_html(
    graph_json: str,
    product_name: str = "",
    final_url: str = "",
    permit_enabled: bool = True,
) -> str:
    return (_TEMPLATE
        .replace("__GRAPH_JSON__", _safe_graph_json(graph_json))
        .replace("__PRODUCT__", _escape_js(product_name))
        .replace("__FINAL_URL__", _escape_js(final_url or "?view=judge_final"))
        .replace("__PERMIT_ENABLED__", "true" if permit_enabled else "false"))


def _escape_js(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<script async src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{min-height:100%;height:auto}
body{font-family:'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',sans-serif;background:#eef5ff;color:#102033;overflow:auto}
#root{display:flex;flex-direction:column;min-height:1100px;height:auto;gap:10px;padding:8px}
#stage{flex:1;position:relative;border-radius:18px;overflow:hidden;min-height:1010px;
  border:1px solid rgba(148,163,184,.55);background:#f8fbff;
  box-shadow:0 18px 44px rgba(15,23,42,.12),inset 0 1px 0 rgba(255,255,255,.82)}
#main-svg{display:block;width:100%;height:100%}
#status-bar{flex:0 0 auto;height:64px;display:flex;align-items:center;justify-content:center;
  gap:18px;padding:0 16px;
  background:linear-gradient(180deg,#ffffff,#eaf3ff);
  border:1px solid rgba(148,163,184,.62);border-radius:12px}
#status-text{font-size:20px;color:#102033;font-weight:900;letter-spacing:.2px}
#summary-panel{display:none!important;align-items:center;gap:16px;padding:8px 14px;border-radius:12px;
  background:#ffffff;border:1px solid rgba(191,215,240,.95);box-shadow:0 6px 18px rgba(15,23,42,.08)}
#summary-panel.on{display:flex}
.summary-title{font-size:13px;font-weight:950;color:#64748b;letter-spacing:.08em;margin-right:2px}
.summary-metric{display:inline-flex;align-items:center;gap:5px;font-size:20px;font-weight:950}
.summary-metric.pass{color:#059669;margin-right:13px}
.summary-metric.hold{color:#f59e0b;margin-right:13px}
.summary-metric.fail{color:#e11d48}
.summary-dot{width:16px;height:16px;border-radius:50%;display:inline-block}
.summary-metric.pass .summary-dot{background:#059669}
.summary-metric.hold .summary-dot{background:#f59e0b}
.summary-metric.fail .summary-dot{background:#e11d48}
#inline-final-link{display:none;align-items:center;justify-content:center;min-height:40px;padding:0 18px;
  border-radius:12px;background:#059669;color:#fff;text-decoration:none;font-weight:950;
  box-shadow:0 8px 18px rgba(5,150,105,.22)}
#inline-final-link.is-visible{display:inline-flex}
@keyframes botPulse{0%,100%{opacity:.22}50%{opacity:.6}}
.bot-aura{animation:botPulse 1.8s ease-in-out infinite}
.bot-group{pointer-events:none}
@keyframes flowDash{to{stroke-dashoffset:-22}}
.edge-flow{fill:none;stroke:#37d99e;stroke-width:2.6;stroke-dasharray:2 14;stroke-linecap:round;
  animation:flowDash 1.1s linear infinite;opacity:0}
.edge-flow.on{opacity:.9}
.start-hint{cursor:pointer}
@keyframes hintPulse{0%,100%{opacity:.3;r:34}50%{opacity:.8;r:40}}
#start-ring{animation:hintPulse 1.4s ease-in-out infinite}
</style>
</head>
<body>
<div id="root">
  <div id="stage">
    <svg id="main-svg" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
      <defs>
        <pattern id="dotgrid" width="24" height="24" patternUnits="userSpaceOnUse">
          <rect width="24" height="24" fill="#f8fbff"/>
          <circle cx="1" cy="1" r="1" fill="rgba(37,99,235,.16)"/>
        </pattern>
        <filter id="glow-bot" x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="9" result="b"/>
          <feFlood flood-opacity="0.3"/><feComposite in2="b" operator="in"/>
          <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <filter id="soft" x="-20%" y="-25%" width="140%" height="150%">
          <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="rgba(15,23,42,0.18)"/>
        </filter>
        <linearGradient id="g-ocr" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#7cc0ff"/><stop offset="100%" stop-color="#55a6ff"/></linearGradient>
        <linearGradient id="g-llm" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#5ee0b4"/><stop offset="100%" stop-color="#37d99e"/></linearGradient>
        <linearGradient id="g-permit" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#c79bf2"/><stop offset="100%" stop-color="#a570e0"/></linearGradient>
        <linearGradient id="g-node" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#ffffff"/><stop offset="100%" stop-color="#edf6ff"/></linearGradient>
        <marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10z" fill="#94a3b8"/>
        </marker>
      </defs>
      <rect id="bg" x="0" y="0" width="100%" height="100%" fill="url(#dotgrid)"/>
      <line id="divider" stroke="rgba(37,99,235,.22)" stroke-width="1.5" stroke-dasharray="6 7"/>
      <g id="edge-layer"></g>
      <g id="flow-layer"></g>
      <g id="box-layer"></g>
      <g id="ind-layer"></g>
      <g id="fx-layer"></g>
      <g id="pipe-layer"></g>
      <g id="bot-layer"></g>
      <g id="hint-layer"></g>
    </svg>
  </div>
  <div id="status-bar">
    <div id="summary-panel" aria-label="summary">
      <span class="summary-title">SUMMARY</span>
      <span class="summary-metric pass"><i class="summary-dot"></i><b id="sum-pass">0</b></span>
      <span class="summary-metric hold"><i class="summary-dot"></i><b id="sum-hold">0</b></span>
      <span class="summary-metric fail"><i class="summary-dot"></i><b id="sum-fail">0</b></span>
    </div>
    <span id="status-text">AI 검수 시뮬레이션을 시작합니다.</span>
  </div>
</div>

<script>
(function(){
const DATA=__GRAPH_JSON__;
if(!DATA || !Array.isArray(DATA.nodes)){
  DATA.nodes=[]; DATA.edges=[]; DATA.layers=[];
}
if(!Array.isArray(DATA.edges))DATA.edges=[];
if(!Array.isArray(DATA.layers))DATA.layers=[];
const PERMIT_ENABLED=__PERMIT_ENABLED__;
const PRODUCT='__PRODUCT__';
const FINAL_URL='__FINAL_URL__';
const C_P='#059669',C_H='#f59e0b',C_F='#e11d48',C_W='#111827',C_B='#2563eb',C_PU='#7c3aed';
const C_LINE='#94a3b8',C_NODE_STROKE='#2563eb',C_TEXT='#102033',C_MUTED='#475569';

// ════════ 캔버스 / 영역 ════════
// viewBox 의 가로세로 비율을 실제 컨테이너 비율에 맞춰, 좌우 여백 없이 박스를 꽉 채운다.
const VH=820, SPLIT=0.63;
const stageEl=document.getElementById('stage');
let _cw=stageEl.clientWidth||(window.innerWidth-16);
let _ch=stageEl.clientHeight||(window.innerHeight-64);
let VW=Math.round(VH*(_cw/Math.max(1,_ch)));
VW=Math.max(1650,Math.min(VW,2600));     // 하단 검증/판정 그룹을 넓게 펼쳐 보이도록 방어
const A_Y0=48, A_Y1=VH*SPLIT-14;
const B_Y0=VH*SPLIT+16, B_Y1=VH-18;
const svg=document.getElementById('main-svg');
svg.setAttribute('viewBox',`0 0 ${VW} ${VH}`);
const divY=VH*SPLIT;
const dv=document.getElementById('divider');
dv.setAttribute('x1',20);dv.setAttribute('x2',VW-20);dv.setAttribute('y1',divY);dv.setAttribute('y2',divY);

function svgEl(t,a){const e=document.createElementNS('http://www.w3.org/2000/svg',t);for(const[k,v]of Object.entries(a||{}))e.setAttribute(k,v);return e}
function fitSvgText(el,maxWidth,minFont){
  requestAnimationFrame(()=>{
    if(!el || typeof el.getComputedTextLength!=='function')return;
    let size=parseFloat(el.getAttribute('font-size'))||21;
    while(size>(minFont||17) && el.getComputedTextLength()>maxWidth){
      size-=1;
      el.setAttribute('font-size',`${size}px`);
    }
    if(el.getComputedTextLength()<=maxWidth)return;
    const full=el.textContent||'';
    let text=full;
    while(text.length>4 && el.getComputedTextLength()>maxWidth){
      text=text.slice(0,-2)+'…';
      el.textContent=text;
    }
  });
}
const edgeL=document.getElementById('edge-layer'),flowL=document.getElementById('flow-layer'),
  boxL=document.getElementById('box-layer'),indL=document.getElementById('ind-layer'),
  fxL=document.getElementById('fx-layer'),pipeL=document.getElementById('pipe-layer'),
  botL=document.getElementById('bot-layer'),hintL=document.getElementById('hint-layer');

// ════════ A : flowchart 레이아웃 (반시계 90° → 좌→우) ════════
const nodesById={};
DATA.nodes.forEach(n=>{nodesById[n.id]=n});
const layers=DATA.layers.filter(l=>l.length);
const cols=layers.length;
const maxLanes=Math.max(1,...layers.map(l=>l.length));
const GRAPH_PAD=96;
const denseGraph=maxLanes>=4;
const maxBoxForCols=cols>1?((VW-GRAPH_PAD*2)/cols-22):282;
const BW=Math.max(denseGraph?176:104,Math.min(denseGraph?276:282,maxBoxForCols)),BH=denseGraph?48:82;
const AX0=GRAPH_PAD+BW/2,AX1=VW-GRAPH_PAD-BW/2;
const colX=i=>cols>1?AX0+i*(AX1-AX0)/(cols-1):(AX0+AX1)/2;
const laneGap=denseGraph?Math.max(BH+25,(A_Y1-A_Y0-92)/Math.max(1,maxLanes-1)):Math.min(178,(A_Y1-A_Y0-112)/maxLanes);
const laneSpan=denseGraph?Math.min(66,laneGap):laneGap;
const aMid=(A_Y0+A_Y1)/2-4;
layers.forEach((layer,li)=>{
  const k=layer.length;
  layer.forEach((nid,lane)=>{
    const nd=nodesById[nid]; if(!nd)return;
    nd._cx=colX(li);
    nd._cy=aMid-(k-1)*laneSpan/2+lane*laneSpan;
    nd._x0=nd._cx-BW/2; nd._y0=nd._cy-BH/2;
    nd._x1=nd._cx+BW/2; nd._y1=nd._cy+BH/2;
  });
});
if(denseGraph){
  const botTopSafety=123; // dense graphs keep bots closer to nodes so the graph stays above the process lane.
  const minNodeTop=Math.min(...DATA.nodes.filter(n=>n._y0!==undefined).map(n=>n._y0));
  const shiftY=Math.max(0,Math.min(74,botTopSafety-minNodeTop));
  if(shiftY>0){
    DATA.nodes.forEach(n=>{
      if(n._y0===undefined)return;
      n._cy+=shiftY; n._y0+=shiftY; n._y1+=shiftY;
    });
  }
}
const topo=[];
layers.forEach(l=>l.forEach(nid=>{if(nodesById[nid]&&nodesById[nid]._cx!==undefined)topo.push(nid)}));
if(!topo.length){
  const msg=svgEl('text',{x:VW/2,y:A_Y0+90,fill:C_MUTED,'font-size':'24px','font-weight':'900','text-anchor':'middle'});
  msg.textContent='제조요약도 시뮬레이션 데이터를 찾지 못했습니다.';
  fxL.appendChild(msg);
  const sub=svgEl('text',{x:VW/2,y:A_Y0+128,fill:C_MUTED,'font-size':'17px','font-weight':'700','text-anchor':'middle'});
  sub.textContent='제품명과 제조요약도 CSV 폴더 매칭 또는 summary.csv를 확인해 주세요.';
  fxL.appendChild(sub);
  const st0=document.getElementById('status-text');
  if(st0)st0.textContent='제조요약도 CSV 노드가 비어 있습니다.';
  return;
}

// ── 연결선 ──
const edgeEls={};
DATA.edges.forEach((e,i)=>{
  const s=nodesById[e.source],t=nodesById[e.target];
  if(!s||!t||s._cx===undefined||t._cx===undefined)return;
  const sx=s._x1,sy=s._cy,tx=t._x0,ty=t._cy,gap=tx-sx;
  const d=`M${sx} ${sy}C${sx+gap*.5} ${sy},${tx-gap*.5} ${ty},${tx} ${ty}`;
  const p=svgEl('path',{fill:'none',stroke:C_LINE,'stroke-width':2,'marker-end':'url(#arrow)',d});
  p.style.opacity=0; edgeL.appendChild(p);
  const f=svgEl('path',{'class':'edge-flow',id:`fl-${i}`,d}); flowL.appendChild(f);
  edgeEls[i]={el:p,flow:f,target:e.target};
});

// ── flowchart 박스 (이름만) ──
const boxes={};
topo.forEach(nid=>{
  const n=nodesById[nid];
  const g=svgEl('g'); g.style.opacity=0;
  const rect=svgEl('rect',{x:n._x0,y:n._y0,width:BW,height:BH,rx:14,fill:'url(#g-node)',
    stroke:C_B,'stroke-width':2,filter:'url(#soft)'});
  const peri=2*(BW+BH); rect.style.strokeDasharray=peri; rect.style.strokeDashoffset=peri;
  g.appendChild(rect);
  g.appendChild(svgEl('circle',{cx:n._x0,cy:n._cy,r:5,fill:'#ffffff',stroke:C_LINE,'stroke-width':1.8}));
  g.appendChild(svgEl('circle',{cx:n._x1,cy:n._cy,r:5,fill:'#ffffff',stroke:C_LINE,'stroke-width':1.8}));
  const label=n.label;
  const tx=svgEl('text',{x:n._cx,y:n._cy,fill:C_TEXT,'font-size':denseGraph?'19px':'22px','font-weight':'900',
    'text-anchor':'middle','dominant-baseline':'central'}); tx.textContent=label;
  g.appendChild(tx);
  boxL.appendChild(g);
  fitSvgText(tx,BW-(denseGraph?18:24),denseGraph?13:14);
  boxes[nid]={g,rect};
});

// ── 분수형 인디케이터 (분자: 적합/보류/부적합, 분모: 전체) ──
const indEls={};
topo.forEach(nid=>{
  const n=nodesById[nid];
  const compactInd=BW<220||denseGraph;
  const g=svgEl('g',{transform:`translate(${n._cx},${n._y1+(denseGraph?8:20)})`}); g.style.opacity=0;
  const denseGap=Math.min(64,BW*.24);
  const cells=denseGraph?[
    {key:'pass',color:C_P,target:n.pass_count,dotX:-denseGap-18,numX:-denseGap+4},
    {key:'hold',color:C_H,target:n.hold_count,dotX:-18,numX:4},
    {key:'fail',color:C_F,target:n.fail_count,dotX:denseGap-18,numX:denseGap+4},
  ]:[
    {key:'pass',color:C_P,target:n.pass_count,dotX:compactInd?-BW*.31:-78,numX:compactInd?-BW*.21:-56},
    {key:'hold',color:C_H,target:n.hold_count,dotX:compactInd?-BW*.02:-8,numX:compactInd?BW*.08:14},
    {key:'fail',color:C_F,target:n.fail_count,dotX:compactInd?BW*.27:64,numX:compactInd?BW*.37:86},
  ];
  const els={};
  cells.forEach(c=>{
    g.appendChild(svgEl('circle',{cx:c.dotX,cy:0,r:denseGraph?5.6:(compactInd?6.2:7.2),fill:'#ffffff',stroke:c.color,'stroke-width':2.2}));
    const dot=g.lastChild;
    const num=svgEl('text',{x:c.numX,y:0,fill:'#475569','font-size':denseGraph?'15px':(compactInd?'17px':'20px'),'font-weight':'950','text-anchor':'middle','dominant-baseline':'central'});
    num.textContent='0'; g.appendChild(num);
    if(c.label){
      const label=svgEl('text',{x:c.labelX,y:0,fill:'#64748b','font-size':'12px','font-weight':'850','dominant-baseline':'central'});
      label.textContent=c.label; g.appendChild(label);
    }
    els[c.key]={dot,num,color:c.color,target:c.target};
  });
  indL.appendChild(g);
  indEls[nid]={g,els};
});
function showInd(nid){gsap.to(indEls[nid].g,{opacity:1,duration:.25})}
function countUp(nid){  // 표시 대상 값으로 카운트업 (허가서가 있으면 after, 없으면 before)
  if(finished)return;
  const n=nodesById[nid];
  const targets=PERMIT_ENABLED?afterCounts(n):null;
  updateSummary(nid, targets || null);
  const info=indEls[nid];
  ['pass','hold','fail'].forEach(k=>{
    const s=info.els[k];
    if(targets)s.target=targets[k];
    gsap.to(s.dot,{attr:{fill:s.color},duration:.3});
    s.num.setAttribute('fill',s.color);
    const p={v:0};
    gsap.to(p,{v:s.target,duration:.7,ease:'power1.out',onUpdate:()=>{if(!finished)s.num.textContent=Math.round(p.v)}});
  });
}
function updateSummary(nid, counts){return;}
function afterCounts(n){
  const value=(afterKey,beforeKey)=>{
    const after=Number(n[afterKey]);
    if(Number.isFinite(after))return after;
    const before=Number(n[beforeKey]);
    return Number.isFinite(before)?before:0;
  };
  return {
    pass:value('after_pass','pass_count'),
    hold:value('after_hold','hold_count'),
    fail:value('after_fail','fail_count'),
    total:value('after_total','total_count')
  };
}
function recountAfter(nid){  // 허가서 검토 후 after 값으로 재집계 (현재값 → after)
  if(finished||!PERMIT_ENABLED)return;
  const n=nodesById[nid],info=indEls[nid];
  const tgt=afterCounts(n);
  n.pass_count=tgt.pass;
  n.hold_count=tgt.hold;
  n.fail_count=tgt.fail;
  n.total_count=tgt.total;
  try{
    document.dispatchEvent(new CustomEvent('sp:permit-after-applied',{
      detail:{nodeId:nid,label:n.label,counts:tgt}
    }));
  }catch(e){}
  ['pass','hold','fail'].forEach(k=>{
    const s=info.els[k];
    s.target=tgt[k];
    const cur=parseInt(s.num.textContent,10)||0;
    if(tgt[k]!==cur){  // 값이 바뀌는 셀은 잠깐 강조 (기본 r=6 유지)
      gsap.fromTo(s.dot,{attr:{r:6}},{attr:{r:9},duration:.28,yoyo:true,repeat:1});
    }
    const p={v:cur};
    gsap.to(p,{v:tgt[k],duration:.7,ease:'power1.out',onUpdate:()=>{if(!finished)s.num.textContent=Math.round(p.v)}});
  });
}

function primeAfterCounts(){  // legacy helper
  if(!PERMIT_ENABLED)return;
  topo.forEach(nid=>{
    const n=nodesById[nid],info=indEls[nid];
    const tgt=afterCounts(n);
    n.pass_count=tgt.pass;
    n.hold_count=tgt.hold;
    n.fail_count=tgt.fail;
    n.total_count=tgt.total;
    if(info){
      ['pass','hold','fail'].forEach(k=>{
        if(info.els[k])info.els[k].target=tgt[k];
      });
    }
  });
}

function verd(n){if(n.fail_count>0)return'fail';if(n.hold_count>0)return'hold';return'pass'}

// ════════ B : 고정 파이프라인 (n8n 스타일) ════════
const PNODE={};
function pipeNode(id,x,y,w,h,icon,name,grad,trigger){
  const g=svgEl('g');
  const rx=trigger?h/2:18;
  const rect=svgEl('rect',{x:x-w/2,y:y-h/2,width:w,height:h,rx:rx,fill:'url(#g-node)',
    stroke:C_NODE_STROKE,'stroke-width':2,filter:'url(#soft)'});
  g.appendChild(rect);
  // 통합 아이콘 (중앙)
  const ic=svgEl('text',{x:x,y:y-2,'font-size':'36px','text-anchor':'middle','dominant-baseline':'central'});
  ic.textContent=icon; g.appendChild(ic);
  // 이름 (n8n: 노드 아래)
  const nm=svgEl('text',{x:x,y:y+h/2+20,fill:C_TEXT,'font-size':'22px','font-weight':'950','text-anchor':'middle','dominant-baseline':'hanging'});
  nm.textContent=name; g.appendChild(nm);
  // 입력/출력 엔드포인트
  if(!trigger)g.appendChild(svgEl('circle',{cx:x-w/2,cy:y,r:7,fill:'#ffffff',stroke:C_LINE,'stroke-width':2}));
  g.appendChild(svgEl('circle',{cx:x+w/2,cy:y,r:7,fill:'#ffffff',stroke:C_LINE,'stroke-width':2}));
  pipeL.appendChild(g);
  PNODE[id]={g,rect,x,y,w,h,baseFill:'url(#g-node)'};
  return PNODE[id];
}
function addStackedText(g,x,y,lines,maxWidth,fontSize,lineGap){
  const arr=Array.isArray(lines)?lines:String(lines).split('\\n');
  const total=(arr.length-1)*lineGap;
  arr.forEach((line,i)=>{
    const t=svgEl('text',{x:x,y:y-total/2+i*lineGap,fill:C_TEXT,'font-size':`${fontSize}px`,'font-weight':'900','text-anchor':'middle','dominant-baseline':'central'});
    t.textContent=line;
    g.appendChild(t);
    fitSvgText(t,maxWidth,13);
  });
}
function pipeChip(id,x,y,w,icon,name,color){
  const h=78;
  const g=svgEl('g');
  const rect=svgEl('rect',{x:x-w/2,y:y-h/2,width:w,height:h,rx:10,fill:'#ffffff',
    stroke:C_NODE_STROKE,'stroke-width':1.8,filter:'url(#soft)'});
  g.appendChild(rect);
  g.appendChild(svgEl('rect',{x:x-w/2,y:y-h/2,width:8,height:h,rx:4,fill:color}));
  const ic=svgEl('text',{x:x,y:y-16,'font-size':'22px','text-anchor':'middle','dominant-baseline':'central'});
  ic.textContent=icon; g.appendChild(ic);
  addStackedText(g,x,y+17,name,w-24,17,18);
  pipeL.appendChild(g);
  PNODE[id]={g,rect,x,y,w,h,color,restStroke:C_NODE_STROKE,restWidth:1.8};
  return PNODE[id];
}
function pipeSegmentGroup(groupId,x,y,w,h,items,accent){
  const g=svgEl('g');
  const outer=svgEl('rect',{x:x-w/2,y:y-h/2,width:w,height:h,rx:12,fill:'#ffffff',
    stroke:'#0f3a5a','stroke-width':2.2,filter:'url(#soft)'});
  g.appendChild(outer);
  const segW=w/items.length;
  items.forEach((item,i)=>{
    const sx=x-w/2+segW*i;
    const cx=sx+segW/2;
    if(i>0){
      g.appendChild(svgEl('line',{x1:sx,y1:y-h/2,x2:sx,y2:y+h/2,stroke:'#0f3a5a','stroke-width':1.8}));
    }
    const seg=svgEl('rect',{x:sx+2,y:y-h/2+2,width:segW-4,height:h-4,rx:i===0||i===items.length-1?9:0,
      fill:'transparent',stroke:'transparent','stroke-width':3});
    g.appendChild(seg);
    const label=Array.isArray(item.lines)?item.lines.join(''):String(item.lines||item.name||'');
    const tx=svgEl('text',{x:cx,y:y,fill:C_TEXT,'font-size':'25px','font-weight':'950',
      'text-anchor':'middle','dominant-baseline':'central'});
    tx.textContent=`${item.icon} ${label}`; g.appendChild(tx);
    fitSvgText(tx,segW-20,18);
    PNODE[item.id]={g,rect:seg,x:cx,y,w:segW,h,color:item.color,restStroke:'transparent',restWidth:3};
  });
  pipeL.appendChild(g);
  return {g,x,y,w,h};
}
function wire(x1,y1,x2,y2,dashed){
  const ln=svgEl('path',{fill:'none',stroke:dashed?'rgba(127,137,140,.55)':C_LINE,'stroke-width':dashed?1.6:2.4,
    d:`M${x1} ${y1}C${x1+(x2-x1)*.5} ${y1},${x2-(x2-x1)*.5} ${y2},${x2} ${y2}`});
  if(dashed)ln.setAttribute('stroke-dasharray','4 5');
  pipeL.insertBefore(ln,pipeL.firstChild);
}
function vwire(x1,y1,x2,y2){  // 수직(아래로) 분기 — n8n 곡선
  const ln=svgEl('path',{fill:'none',stroke:'rgba(127,137,140,.5)','stroke-width':1.6,'stroke-dasharray':'4 5',
    d:`M${x1} ${y1}C${x1} ${y1+(y2-y1)*.5},${x2} ${y2-(y2-y1)*.5},${x2} ${y2}`});
  pipeL.insertBefore(ln,pipeL.firstChild);
}
function branchWire(parentId, childIds, opts){
  const parent=PNODE[parentId];
  const children=childIds.map(id=>PNODE[id]).filter(Boolean);
  if(!parent || !children.length)return;
  const color=(opts&&opts.color)||'rgba(17,24,39,.82)';
  const startY=parent.y+parent.h/2+((opts&&opts.startOffset)||8);
  const childTop=Math.min(...children.map(c=>c.y-c.h/2));
  const minX=Math.min(parent.x,...children.map(c=>c.x));
  const maxX=Math.max(parent.x,...children.map(c=>c.x));
  let busY=(opts&&Number.isFinite(opts.busY))?opts.busY:(startY+Math.max(10,Math.min(26,(childTop-startY)*.55)));
  busY=Math.max(startY+6,Math.min(busY,childTop-8));
  const paths=[
    `M${parent.x} ${startY}L${parent.x} ${busY}`,
    `M${minX} ${busY}L${maxX} ${busY}`,
    ...children.map(c=>`M${c.x} ${busY}L${c.x} ${c.y-c.h/2}`)
  ];
  paths.forEach(d=>{
    const ln=svgEl('path',{fill:'none',stroke:color,'stroke-width':1.8,'stroke-dasharray':'5 7','stroke-linecap':'round',d});
    pipeL.insertBefore(ln,pipeL.firstChild);
  });
}

// 행 좌표 (가로 위치는 VW 비례 → 폭을 꽉 채움)
const railY=B_Y0+46, groupY=B_Y0+188;
const PNW=180,PNH=76;
pipeNode('start',VW*0.08,railY,PNW,PNH,'▷','Start','g-ocr',true);
pipeNode('structure',VW*0.28,railY,PNW+52,PNH,'🔍','구조분석','g-ocr');
pipeNode('validate',VW*0.49,railY,PNW+8,PNH,'🛡️','검증','g-llm');
pipeNode('judge',VW*0.70,railY,PNW+8,PNH,'🧠','판정','g-permit');
pipeNode('end',VW*0.91,railY,PNW,PNH,'🏁','End','g-llm');

// End 초록 박스 전체 클릭 영역.
// 사용자가 보는 초록 rounded rectangle 자체를 누르면 최종 판정 화면으로 이동한다.
let endNavigationReady = false;
let endBoxHit = null;
function revealFinalJudgementLink(){
  enableEndBoxNavigation();
  let revealedInParent = false;
  try{
    window.parent.postMessage({type:'sp-sim-finished', finalUrl:FINAL_URL}, '*');
  }catch(e){}
  try{
    const pdoc = window.parent && window.parent.document;
    if(pdoc){
      try{pdoc.body.classList.add('sp-sim-finished');}catch(e){}
      const links = pdoc.querySelectorAll('a.final-judge-after-sim');
      links.forEach((link)=>{
        link.setAttribute('href', FINAL_URL);
        link.classList.add('is-visible');
      });
      revealedInParent = links.length > 0;
    }
  }catch(e){
    revealedInParent = false;
  }

  if(!revealedInParent){
    const bar = document.getElementById('status-bar');
    let link = document.getElementById('inline-final-link');
    if(bar && !link){
      link = document.createElement('a');
      link.id = 'inline-final-link';
      link.target = '_top';
      link.textContent = '최종 판정 페이지로 이동';
      bar.appendChild(link);
    }
    if(link){
      link.href = FINAL_URL;
      link.classList.add('is-visible');
    }
  }
  st.textContent='검수 완료 — 최종 판정 페이지로 이동할 수 있습니다';
}
function goFinalJudgementFromEndBox(event){
  if(event){
    try{event.preventDefault();}catch(e){}
    try{event.stopPropagation();}catch(e){}
  }

  if(!endNavigationReady){
    st.textContent='검수가 완료되면 초록 End 박스를 눌러 최종 판정 화면으로 이동할 수 있습니다';
    return false;
  }

  st.textContent='아래 최종 판정 페이지로 이동 버튼을 눌러 결과를 확인하세요';
  return false;
}
function enableEndBoxNavigation(){
  endNavigationReady = true;
  const endP = PNODE['end'];
  if(!endP)return;
  try{endP.g.style.cursor='pointer';}catch(e){}
  try{endP.rect.setAttribute('data-clickable','true');}catch(e){}
}
function installEndBoxClickArea(){
  const endP = PNODE['end'];
  if(!endP || endBoxHit)return;

  // 여기 크기가 실제 End 초록 박스 크기다. 글씨가 아니라 박스 전체를 덮는다.
  endBoxHit = svgEl('rect',{
    x:endP.x-endP.w/2,
    y:endP.y-endP.h/2,
    width:endP.w,
    height:endP.h,
    rx:16,
    fill:'transparent',
    'data-end-box-hit':'1'
  });
  endBoxHit.style.cursor='pointer';
  endBoxHit.style.pointerEvents='all';

  endBoxHit.addEventListener('click',goFinalJudgementFromEndBox,true);
  endBoxHit.addEventListener('mouseenter',()=>{
    try{endP.rect.setAttribute('stroke',C_P);}catch(e){}
    try{endP.rect.setAttribute('stroke-width',3.4);}catch(e){}
    try{endP.g.style.filter='drop-shadow(0 0 18px '+C_P+'cc)';}catch(e){}
  });
  endBoxHit.addEventListener('mouseleave',()=>{
    if(endNavigationReady){
      try{endP.rect.setAttribute('stroke',C_P);}catch(e){}
      try{endP.rect.setAttribute('stroke-width',3.4);}catch(e){}
      try{endP.g.style.filter='drop-shadow(0 0 20px '+C_P+'cc)';}catch(e){}
    }else{
      try{endP.rect.setAttribute('stroke',C_NODE_STROKE);}catch(e){}
      try{endP.rect.setAttribute('stroke-width',2);}catch(e){}
      try{endP.g.style.filter='';}catch(e){}
    }
  });

  // hint-layer는 pipe-layer보다 뒤에 있으므로 End 박스 위에 정확히 클릭 판이 올라간다.
  hintL.appendChild(endBoxHit);

  try{endP.g.addEventListener('click',goFinalJudgementFromEndBox,true);}catch(e){}
}
installEndBoxClickArea();
// rail 연결선 + 흐름
[['start','structure'],['structure','validate'],['validate','judge'],['judge','end']].forEach(([a,b],i)=>{
  const A=PNODE[a],B=PNODE[b];
  wire(A.x+A.w/2,A.y,B.x-B.w/2,B.y,false);
  const fd=`M${A.x+A.w/2} ${A.y}C${A.x+A.w/2+(B.x-A.x)*.5} ${A.y},${B.x-B.w/2-(B.x-A.x)*.5} ${B.y},${B.x-B.w/2} ${B.y}`;
  const flow=svgEl('path',{'class':'edge-flow',d:fd,id:`pwf-${i}`}); pipeL.appendChild(flow);
});
// 검증 로직 그룹
const valX=PNODE['validate'].x;
const groupH=66;
const GROUP_PAD=92;
const GROUP_GAP=Math.max(76,Math.min(118,VW*.055));
const GROUP_AVAILABLE=Math.max(1120,VW-GROUP_PAD*2-GROUP_GAP);
const valW=Math.min(760,Math.max(560,GROUP_AVAILABLE*.49));
const valGroupX=GROUP_PAD+valW/2;
pipeSegmentGroup('validation-group',valGroupX,groupY,valW,groupH,[
  {id:'pre',icon:'↕',lines:['선행 검증'],color:C_B},
  {id:'mfg',icon:'🧾',lines:['제조량 검증'],color:C_P},
  {id:'mfg_date',icon:'📆',lines:['제조일자 검증'],color:C_H},
  {id:'mfg_no',icon:'🔢',lines:['제조번호 검증'],color:C_P}
],'rgba(37,99,235,.42)');
branchWire('validate',['pre','mfg','mfg_date','mfg_no'],{startOffset:52,color:'rgba(17,24,39,.92)',busY:groupY-68});
const valCaption=svgEl('text',{x:valGroupX,y:groupY+groupH/2+42,fill:C_TEXT,'font-size':'30px','font-weight':'950','text-anchor':'middle'});
valCaption.textContent='✹ 정합성 검증 로직'; pipeL.appendChild(valCaption);

// 판정 참조 정보 그룹
const judgeX=PNODE['judge'].x;
const refW=Math.min(820,Math.max(600,GROUP_AVAILABLE-valW));
const judgeGroupX=GROUP_PAD+valW+GROUP_GAP+refW/2;
const refY=groupY;
pipeSegmentGroup('judge-ref-group',judgeGroupX,refY,refW,groupH,[
  {id:'pharm',icon:'📘',lines:['약전'],color:C_B},
  {id:'permit',icon:'📑',lines:['허가서'],color:C_PU},
  {id:'sym',icon:'⚖',lines:['기호사전'],color:C_H},
  {id:'bio',icon:'📗',lines:['생기법'],color:C_P}
],'rgba(37,99,235,.42)');
branchWire('judge',['pharm','permit','sym','bio'],{startOffset:52,color:'rgba(17,24,39,.92)',busY:refY-68});
const judgeCaption=svgEl('text',{x:judgeGroupX,y:refY+groupH/2+42,fill:C_TEXT,'font-size':'30px','font-weight':'950','text-anchor':'middle'});
judgeCaption.textContent='📘 판정 근거 정보'; pipeL.appendChild(judgeCaption);

// ── 점등 헬퍼 ──
function railSet(activeId,color){
  ['start','structure','validate','judge','end'].forEach(id=>{
    const p=PNODE[id];
    if(id===activeId){gsap.to(p.rect,{attr:{stroke:color,'stroke-width':3.2},duration:.3});p.g.style.filter='drop-shadow(0 0 16px '+color+'aa)';}
    else{gsap.to(p.rect,{attr:{stroke:C_NODE_STROKE,'stroke-width':2,fill:p.baseFill},duration:.3});p.g.style.filter='';}
  });
}
function railFinish(){
  railSet('end',C_P);
  gsap.to(PNODE['end'].rect,{attr:{fill:'rgba(55,217,158,.20)','stroke':C_P,'stroke-width':3.4},duration:.45});
  PNODE['end'].g.style.filter='drop-shadow(0 0 20px '+C_P+'cc)';
  enableEndBoxNavigation();
}
function chipGlow(id,color){const p=PNODE[id];const c=color||p.color;p.rect.setAttribute('stroke',c);p.rect.setAttribute('stroke-width',2.8);p.g.style.filter='drop-shadow(0 0 14px '+c+'cc)';}
function chipDim(id){const p=PNODE[id];p.rect.setAttribute('stroke',p.restStroke||C_NODE_STROKE);p.rect.setAttribute('stroke-width',p.restWidth||1.8);p.g.style.filter='';}

// ════════ 봇 ════════
function createBot(gradId,accent,label){
  const g=svgEl('g',{'class':'bot-group'}); g.style.opacity=0;
  g.appendChild(svgEl('circle',{cx:0,cy:0,r:58,fill:'none',stroke:accent,'stroke-width':2.8,opacity:.22,'class':'bot-aura'}));
  g.appendChild(svgEl('rect',{x:-36,y:-42,width:72,height:66,rx:18,fill:`url(#${gradId})`,stroke:'rgba(255,255,255,.24)','stroke-width':2.8,filter:'url(#glow-bot)'}));
  g.appendChild(svgEl('line',{x1:0,y1:-42,x2:0,y2:-58,stroke:'rgba(255,255,255,.55)','stroke-width':2.8,'stroke-linecap':'round'}));
  g.appendChild(svgEl('circle',{cx:0,cy:-61,r:6,fill:accent}));
  g.appendChild(svgEl('circle',{cx:-13,cy:-16,r:6.2,fill:'rgba(255,255,255,.9)'}));
  g.appendChild(svgEl('circle',{cx:13,cy:-16,r:6.2,fill:'rgba(255,255,255,.9)'}));
  g.appendChild(svgEl('circle',{cx:-10,cy:-16,r:3.2,fill:'#0f172a'}));
  g.appendChild(svgEl('circle',{cx:16,cy:-16,r:3.2,fill:'#0f172a'}));
  g.appendChild(svgEl('path',{d:'M-11 8Q0 17 11 8',fill:'none',stroke:'rgba(255,255,255,.66)','stroke-width':2.8,'stroke-linecap':'round'}));
  const nm=svgEl('text',{x:0,y:44,fill:accent,'font-size':'17px','font-weight':'900','text-anchor':'middle','dominant-baseline':'hanging'});
  nm.textContent=label; g.appendChild(nm);
  botL.appendChild(g); return g;
}
const structureBot=createBot('g-ocr',C_B,'구조분석봇');
const validateBot=createBot('g-llm',C_P,'검증봇');
const judgeBot=createBot('g-permit',C_PU,'판정봇');

const st=document.getElementById('status-text');

// ════════ 타임라인 ════════
const MOVE=0.34,DRAW=0.42,OCR_STEP=0.58,REV=0.45,BOT_OFFSET=72,TOP_BOT_OFFSET=denseGraph?44:72;
let built=false,finished=false,running=false,tl=null,finishTimer=null,presentationStarted=0;
const MAX_PRESENTATION_SECONDS=12;

function finishPresentation(){
  if(finished)return;
  finished=true; running=false;
  clearTimeout(finishTimer);
  if(tl)tl.kill();
  // Counts are already computed by Python. Animation failure must not hide
  // them or manufacture a new verdict, including the no-permit path.
  topo.forEach(nid=>{
    const n=nodesById[nid],info=indEls[nid];
    boxes[nid].g.style.opacity=1;
    boxes[nid].rect.style.strokeDashoffset=0;
    info.g.style.opacity=1;
    const counts=PERMIT_ENABLED?afterCounts(n):{pass:n.pass_count,hold:n.hold_count,fail:n.fail_count};
    ['pass','hold','fail'].forEach(k=>{
      info.els[k].num.textContent=counts[k];
      info.els[k].num.setAttribute('fill',info.els[k].color);
      info.els[k].dot.setAttribute('fill',info.els[k].color);
    });
  });
  Object.values(edgeEls).forEach(e=>{e.el.style.opacity=1;});
  [structureBot,validateBot,judgeBot].forEach(bot=>{bot.style.opacity=0;});
  document.getElementById('pwf-3').classList.add('on');
  enableEndBoxNavigation();
  st.textContent='검수 완료 — 최종 판정 페이지로 이동할 수 있습니다';
  st.dataset.presentationMs=String(Math.round(performance.now()-presentationStarted));
  revealFinalJudgementLink();
}

function buildTimeline(){
  const tl=gsap.timeline({paused:true});
  let t=0;

  // ── Start ──
  tl.call(()=>{railSet('start',C_B);st.textContent='제조 요약도 입력 — 검수 시작';},[],t);
  t+=.6;
  tl.call(()=>{railSet('structure',C_B);document.getElementById('pwf-0').classList.add('on');},[],t);

  // ── Phase 1 : 구조분석봇 작도 (좌→우) ──
  const fn=nodesById[topo[0]];
  tl.set(structureBot,{x:fn._cx,y:fn._y0-TOP_BOT_OFFSET,opacity:0},t);
  tl.to(structureBot,{opacity:1,duration:.4},t); t+=.4;
  topo.forEach(nid=>{
    const n=nodesById[nid],bx=boxes[nid];
    tl.to(structureBot,{x:n._cx,y:n._y0-TOP_BOT_OFFSET,duration:MOVE,ease:'power2.inOut',
      onStart:()=>{st.textContent=`구조분석봇: 제조 요약도 구조 작도 — ${n.label}`}},t);
    DATA.edges.forEach((e,ei)=>{if(e.target===nid&&edgeEls[ei])tl.to(edgeEls[ei].el,{opacity:1,duration:.3},t)});
    tl.to(bx.g,{opacity:1,duration:.2},t+MOVE);
    tl.to(bx.rect,{strokeDashoffset:0,duration:DRAW,ease:'power1.inOut'},t+MOVE);
    t+=OCR_STEP;
  });
  tl.to(structureBot,{opacity:0,duration:.3},t);
  t+=.25;

  // ── Phase 2 : 검증봇 — 구조분석 결과를 노드별로 검증 ──
  tl.call(()=>{railSet('validate',C_P);document.getElementById('pwf-1').classList.add('on');},[],t);
  t+=.5;
  const jn=nodesById[topo[0]];
  tl.set(validateBot,{x:jn._cx,y:jn._y0-TOP_BOT_OFFSET,opacity:0},t);
  tl.to(validateBot,{opacity:1,duration:.35},t); t+=.35;
  tl.call(()=>{DATA.edges.forEach((e,ei)=>{if(edgeEls[ei])edgeEls[ei].flow.classList.add('on')})},[],t);
  const pre=PNODE['pre'],mfg=PNODE['mfg'],mfgDate=PNODE['mfg_date'],mfgNo=PNODE['mfg_no'];
  topo.forEach(nid=>{
    const n=nodesById[nid];
    tl.to(validateBot,{x:n._cx,y:n._y0-TOP_BOT_OFFSET,duration:MOVE,ease:'power2.inOut',
      onStart:()=>{st.textContent=`검증봇: 구조분석 박스 확인 — ${n.label}`}},t);
    tl.fromTo(boxes[nid].rect,{},{attr:{stroke:C_P,'stroke-width':3.2},duration:.2,yoyo:true,repeat:1},'>');
    tl.to(validateBot,{x:pre.x,y:pre.y-50,duration:.24,ease:'power2.inOut',
      onStart:()=>{chipGlow('pre');st.textContent=`검증봇: 선행검증 (${n.label})`}},'>');
    tl.to(validateBot,{x:mfg.x,y:mfg.y-50,duration:.24,ease:'power2.inOut',
      onStart:()=>{chipDim('pre');chipGlow('mfg');st.textContent=`검증봇: 제조검증 (${n.label})`}},'>');
    tl.to(validateBot,{x:mfgDate.x,y:mfgDate.y-50,duration:.24,ease:'power2.inOut',
      onStart:()=>{chipDim('mfg');chipGlow('mfg_date');st.textContent=`검증봇: 제조일자 검증 (${n.label})`}},'>');
    tl.to(validateBot,{x:mfgNo.x,y:mfgNo.y-50,duration:.24,ease:'power2.inOut',
      onStart:()=>{chipDim('mfg_date');chipGlow('mfg_no');st.textContent=`검증봇: 제조번호 검증 (${n.label})`}},'>');
    tl.call(()=>chipDim('mfg_no'),[], '>');
    t+=1.56;
  });
  tl.to(validateBot,{opacity:0,duration:.3},t);
  t+=.4;

  // ── Phase 3 : 판정봇 — 판정 참조 정보를 노드별로 확인하고 SUMMARY 갱신 ──
  tl.call(()=>{railSet('judge',C_PU);document.getElementById('pwf-2').classList.add('on');},[],t);
  tl.set(judgeBot,{x:jn._cx,y:jn._y0-TOP_BOT_OFFSET,opacity:0},t);
  tl.to(judgeBot,{opacity:1,duration:.35},t);
  t+=.45;
  const pharm=PNODE['pharm'],permit=PNODE['permit'],sym=PNODE['sym'],bio=PNODE['bio'];
  topo.forEach(nid=>{
    const n=nodesById[nid];
    tl.to(judgeBot,{x:n._cx,y:n._y0-TOP_BOT_OFFSET,duration:MOVE,ease:'power2.inOut',
      onStart:()=>{updateSummary(nid);st.textContent=`판정봇: 판정 대상 확인 — ${n.label}`}},t);
    tl.fromTo(boxes[nid].rect,{},{attr:{stroke:C_PU,'stroke-width':3.2},duration:.2,yoyo:true,repeat:1},'>');
    tl.to(judgeBot,{x:pharm.x,y:pharm.y-50,duration:.24,ease:'power2.inOut',
      onStart:()=>{chipGlow('pharm');updateSummary(nid);st.textContent=`판정봇: 약전 확인 (${n.label})`}},'>');
    tl.to(judgeBot,{x:permit.x,y:permit.y-50,duration:.24,ease:'power2.inOut',
      onStart:()=>{chipDim('pharm');chipGlow('permit');updateSummary(nid);st.textContent=`판정봇: 허가서 확인 (${n.label})`}},'>');
    tl.to(judgeBot,{x:sym.x,y:sym.y-50,duration:.24,ease:'power2.inOut',
      onStart:()=>{chipDim('permit');chipGlow('sym');updateSummary(nid);st.textContent=`판정봇: 기호사전 확인 (${n.label})`}},'>');
    tl.to(judgeBot,{x:bio.x,y:bio.y-50,duration:.26,ease:'power2.inOut',
      onStart:()=>{chipDim('sym');chipGlow('bio');updateSummary(nid);st.textContent=`판정봇: 생기법 확인 (${n.label})`}},'>');
    tl.to(judgeBot,{x:n._cx,y:n._y0-TOP_BOT_OFFSET,duration:.25,ease:'power2.inOut',
      onStart:()=>chipDim('bio')},'>');
    t+=1.46;
    tl.call(()=>{showInd(nid);countUp(nid);st.textContent=`판정봇: SUMMARY 갱신 — ${n.label}`;},[],t);
    t+=.58;
  });
  tl.to(judgeBot,{opacity:0,duration:.3},t);
  t+=.4;

  // ── 마무리: End 만 색칠 ──
  tl.call(()=>{if(PERMIT_ENABLED)finalizeAfter();document.getElementById('pwf-3').classList.add('on');railFinish();
    st.textContent='✅ 검수 완료 — 구조분석, 검증, 판정 완료';},[],t+.6);
  return tl;
}

function finalizeAfter(){  // 종료 시 전 노드의 표시를 after 최종값으로 확정
  if(!PERMIT_ENABLED)return;
  try{
    document.dispatchEvent(new CustomEvent('sp:permit-after-finalized'));
  }catch(e){}
  topo.forEach(nid=>{
    const n=nodesById[nid],info=indEls[nid];
    info.g.style.opacity=1;
    const tgt=afterCounts(n);
    n.pass_count=tgt.pass;
    n.hold_count=tgt.hold;
    n.fail_count=tgt.fail;
    n.total_count=tgt.total;
    ['pass','hold','fail'].forEach(k=>{
      const s=info.els[k];
      s.num.textContent=tgt[k];
      s.num.setAttribute('fill',s.color);
      s.dot.setAttribute('fill',s.color);
    });
  });
}

// ── Start 노드 클릭으로 시작 ──
const startP=PNODE['start'];
const ring=svgEl('circle',{id:'start-ring',cx:startP.x,cy:startP.y,r:44,fill:'none',stroke:C_B,'stroke-width':3,opacity:.5});
hintL.appendChild(ring);
// 클릭 영역
const hit=svgEl('rect',{x:startP.x-startP.w/2,y:startP.y-startP.h/2,width:startP.w,height:startP.h,rx:startP.h/2,fill:'transparent','class':'start-hint'});
hintL.appendChild(hit);
function begin(){
  if(running)return;
  if(finished){location.reload();return;}
  running=true;
  presentationStarted=performance.now();
  hintL.style.display='none';
  // CDN unavailable/offline: show the actual final graph without animation.
  if(typeof gsap==='undefined'){finishPresentation();return;}
  try{
    if(!built){tl=buildTimeline();built=true;}
    tl.timeScale(Math.max(1,tl.duration()/MAX_PRESENTATION_SECONDS));
    tl.eventCallback('onComplete',finishPresentation);
    finishTimer=setTimeout(finishPresentation,(MAX_PRESENTATION_SECONDS+2)*1000);
    tl.play();
  }catch(error){finishPresentation();}
}
hit.addEventListener('click',begin);
hit.addEventListener('mouseenter',()=>{startP.rect.setAttribute('stroke',C_B);startP.rect.setAttribute('stroke-width',2.6);startP.g.style.filter='drop-shadow(0 0 12px '+C_B+'aa)';});
hit.addEventListener('mouseleave',()=>{if(!running){startP.rect.setAttribute('stroke',C_NODE_STROKE);startP.rect.setAttribute('stroke-width',1.6);startP.g.style.filter='';}});
})();
</script>
</body>
</html>

"""
