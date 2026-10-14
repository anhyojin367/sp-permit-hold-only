"""Execute counter callbacks without a browser to model late independent tweens."""
import json
import shutil
import subprocess

import pytest

from sp_sim2_viewer import build_simulation_html


@pytest.mark.parametrize('permit', [False, True])
@pytest.mark.parametrize('method', ['countUp', 'recountAfter'])
def test_late_counter_callbacks_cannot_overwrite_completed_counts(permit, method):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for the isolated JavaScript callback test')
    html = build_simulation_html('{"nodes":[],"edges":[]}')
    functions = html[html.index('function countUp('):html.index('function primeAfterCounts(')]
    finish = html[html.index('function finishPresentation('):html.index('function buildTimeline(')]
    setup = '''
const assert=require('node:assert/strict');
const callbacks=[];
const gsap={to(target, options){if(options.onUpdate)callbacks.push(()=>{target.v=3;options.onUpdate()});},fromTo(){}};
const element=()=>({style:{},textContent:'0',setAttribute(){},classList:{add(){}}});
const document={getElementById:()=>element(),dispatchEvent(){}};
const topo=['a'];
const nodesById={a:{pass_count:12,hold_count:1,fail_count:2,total_count:15,after_pass:32,after_hold:2,after_fail:4,after_total:38}};
const indEls={a:{g:element(),els:Object.fromEntries(['pass','hold','fail'].map(k=>[k,{num:element(),dot:element(),color:'green',target:nodesById.a[k+'_count']}]))}};
const boxes={a:{g:element(),rect:element()}},edgeEls={};
const structureBot=element(),validateBot=element(),judgeBot=element();
const st={textContent:'',dataset:{}},tl={kill(){}};
let finished=false,running=true,finishTimer=null,presentationStarted=performance.now();
function enableEndBoxNavigation(){}
function revealFinalJudgementLink(){}
'''
    verify = f'''
{method}('a');
callbacks.forEach(update=>update());
finishPresentation();
const expected=PERMIT_ENABLED?[32,2,4]:[12,1,2];
const values=()=>['pass','hold','fail'].map(k=>Number(indEls.a.els[k].num.textContent));
assert.deepEqual(values(),expected);
callbacks.forEach(update=>update());
assert.deepEqual(values(),expected,'late update overwrote a completed verdict count');
{method}('a');
callbacks.forEach(update=>update());
assert.deepEqual(values(),expected,'late counter start overwrote a completed verdict count');
'''
    code = f'const PERMIT_ENABLED={json.dumps(permit)};\n' + setup + functions + finish + verify
    completed = subprocess.run([node, '-e', code], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
