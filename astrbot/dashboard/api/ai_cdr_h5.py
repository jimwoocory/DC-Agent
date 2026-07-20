"""Dependency-free Feishu H5 surface for the AI-to-CDR conversion queue."""

from __future__ import annotations

import json


def build_ai_cdr_page(token: str) -> str:
    """Render the batch upload, color calibration, and progress workspace.

    Args:
        token: Capability token already validated by the parent route.

    Returns:
        Self-contained HTML served inside the Feishu sidebar web app.
    """
    token_json = json.dumps(token, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN" data-ui-system="material-quotation">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#46513a">
  <title>AI 转 CDR</title>
  <style>
    :root{{--ink:#25281f;--muted:#777a6d;--line:#dedfd6;--paper:#ffffff;--panel:#ffffff;--olive:#46513a;--olive-2:#657158;--signal:#d96b32;--signal-soft:#fff0e6;--ok:#347558;--warn:#a75225;--soft:#faf8ee;--blue:var(--signal);--blue-soft:var(--signal-soft);--green:var(--ok);--red:var(--warn);--orange:var(--signal);--technical:"Microsoft YaHei","PingFang SC",sans-serif}}
    *{{box-sizing:border-box}}html,body{{margin:0;min-height:100%;background:var(--paper);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC",sans-serif}}
    button,input,select{{font:inherit}}button{{cursor:pointer}}.app{{max-width:1100px;margin:0 auto;padding:18px 18px 42px}}
    .hero{{position:relative;display:flex;justify-content:space-between;gap:16px;align-items:flex-start;padding:8px 0 20px 18px;border-bottom:1px solid var(--line)}}.hero::before{{content:"";position:absolute;left:0;top:8px;bottom:20px;width:5px;background:var(--signal)}}
    .eyebrow{{font:900 11px/1 var(--technical);letter-spacing:.16em;color:var(--signal);text-transform:uppercase}}h1{{font:800 30px/1.08 "Songti SC","STSong",serif;letter-spacing:-.04em;margin:7px 0 8px}}.hero p{{margin:0;color:var(--muted);line-height:1.6;font-size:13px}}
    .state-chip{{white-space:nowrap;border:1px solid #d8c5ae;border-radius:99px;background:#fff7ed;padding:7px 10px;font:900 12px/1 var(--technical);letter-spacing:.03em;color:#865126}}
    .steps{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:16px 0}}.step{{border:1px solid var(--line);border-top:3px solid var(--olive-2);border-radius:10px;padding:10px 12px;background:#fff;font-size:12px;color:var(--muted)}}.step:nth-child(2){{border-top-color:var(--olive)}}.step:nth-child(3){{border-top-color:var(--signal)}}.step:nth-child(4){{border-top-color:var(--ok)}}.step strong{{display:block;color:var(--ink);font:900 13px/1.2 var(--technical);letter-spacing:.02em;margin-bottom:4px}}
    .card{{border:1px solid var(--line);border-radius:12px;padding:16px;margin-top:14px;background:#fff;box-shadow:0 5px 16px rgba(61,59,43,.05);animation:enter .32s ease both}}.card:nth-of-type(3){{animation-delay:.05s}}.card:nth-of-type(4){{animation-delay:.1s}}.card-head{{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:13px}}h2{{font:800 18px/1.2 "Songti SC","STSong",serif;letter-spacing:-.03em;margin:0}}.hint{{font-size:12px;color:var(--muted);line-height:1.55}}
    .drop{{position:relative;display:block;width:100%;border:1.5px dashed #cfd2c7;border-radius:10px;background:#fafbf8;padding:24px 18px;text-align:center;transition:.15s}}.drop.drag{{border-color:var(--signal);background:var(--signal-soft)}}.drop input{{position:absolute;inset:0;opacity:0;width:100%;cursor:pointer}}.drop b{{display:block;margin-bottom:5px;font:900 15px/1.2 var(--technical)}}.drop span{{font-size:12px;color:var(--muted)}}
    .file-list,.job-list{{display:grid;gap:8px;margin-top:12px}}.file-row,.job-row{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--line);border-left:3px solid var(--olive);border-radius:10px;padding:10px 12px;background:#fff}}.file-name{{font-size:13px;font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.file-meta{{font:500 11px/1.3 var(--technical);color:var(--muted);margin-top:3px}}.remove{{border:0;background:transparent;color:var(--warn);padding:5px}}
    .toolbar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}select,.field{{border:1px solid var(--line);border-radius:9px;background:#fff;min-height:36px;padding:6px 9px;color:var(--ink)}}select{{min-width:220px}}.secondary{{border:1px solid #cfd2c7;background:#f7f8f2;border-radius:9px;padding:8px 12px;color:var(--olive);font-weight:900}}.secondary:hover{{border-color:var(--olive);color:var(--olive)}}.primary{{border:0;background:var(--signal);color:#fff;border-radius:10px;padding:12px 20px;font-weight:900;box-shadow:0 8px 18px rgba(217,107,50,.2)}}.primary:hover{{background:#b95426}}.primary:disabled{{opacity:.5;cursor:not-allowed}}
    .mapping-list{{display:grid;gap:10px;margin-top:12px}}.mapping{{border:1px solid var(--line);border-radius:10px;padding:12px;background:#fafbf8}}.mapping-top{{display:grid;grid-template-columns:minmax(130px,1fr) 150px auto;gap:8px;align-items:center}}.mapping-top .field{{width:100%}}.delete{{border:0;background:transparent;color:var(--red);padding:6px}}
    .values{{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center;margin-top:10px}}.color-block>span{{display:block;margin-bottom:5px;font:700 11px/1 var(--technical);letter-spacing:.04em;color:var(--muted)}}.source-picker{{display:grid;grid-template-columns:30px minmax(0,1fr);gap:8px;align-items:center}}.swatch{{width:30px;height:30px;border:1px solid rgba(70,81,58,.22);border-radius:8px;background:var(--swatch,#fff);box-shadow:inset 0 0 0 1px rgba(255,255,255,.45)}}.source-picker select{{min-width:0;width:100%}}.source-values{{grid-column:1/3;font:650 11px/1.35 var(--technical);color:var(--muted);letter-spacing:.02em}}.cmyk{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}}.cmyk label{{font:650 10px/1 var(--technical);letter-spacing:.06em;color:var(--muted);text-align:center}}.cmyk input{{width:100%;min-width:0;border:1px solid var(--line);border-radius:8px;padding:8px 5px;text-align:center;font-variant-numeric:tabular-nums}}.arrow{{color:var(--muted)}}
    .empty{{padding:20px;text-align:center;color:var(--muted);font-size:13px;background:var(--soft);border-radius:10px}}
    .icc{{display:grid;grid-template-columns:auto minmax(0,1fr);gap:12px;align-items:center;margin-top:14px;padding:13px;border-radius:12px;background:#faf8ee}}.icc-picker{{position:relative;display:inline-flex}}.icc-picker input{{position:absolute;inset:0;opacity:0;cursor:pointer}}.icc-status{{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.notice{{margin-top:8px;padding-left:9px;border-left:3px solid var(--signal);font-size:11px;color:var(--warn);line-height:1.55}}
    .submit-row{{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-top:14px}}.status{{font-size:13px;color:var(--muted);line-height:1.5}}.status.bad{{color:var(--red)}}.status.ok{{color:var(--green)}}
    .progress-summary{{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:12px;align-items:center}}.bar{{height:9px;border-radius:99px;background:#e4e5dd;overflow:hidden}}.bar>i{{display:block;height:100%;width:0;background:var(--signal);transition:width .35s ease}}.percent{{font:900 15px/1 var(--technical);font-variant-numeric:tabular-nums}}.time{{font:700 12px/1 var(--technical);color:var(--muted);white-space:nowrap}}.job-row{{grid-template-columns:minmax(0,1fr) 190px 48px}}.job-state{{font-size:12px;color:var(--muted);margin-bottom:6px}}.job-state.failed{{color:var(--red)}}.job-state.succeeded{{color:var(--green)}}.job-percent{{font:900 12px/1 var(--technical);text-align:right;font-variant-numeric:tabular-nums}}
    .download{{display:inline-flex;align-items:center;margin:7px 0 0;border:1px solid var(--olive);border-radius:8px;background:#fff;color:var(--olive);padding:6px 10px;font-size:12px;font-weight:900}}.download:hover{{background:#f7f8f2}}.download:disabled{{opacity:.55;cursor:wait}}.download-result{{display:block;margin-top:5px;font-size:11px;color:var(--muted)}}.download-result.ok{{color:var(--green)}}.download-result.bad{{color:var(--red)}}.footer{{display:flex;justify-content:flex-end;margin-top:16px}}[hidden]{{display:none!important}}@keyframes enter{{from{{opacity:0;transform:translateY(6px)}}to{{opacity:1;transform:none}}}}@media(prefers-reduced-motion:reduce){{.card{{animation:none}}.bar>i{{transition:none}}}}
    @media(max-width:720px){{.app{{padding:12px 12px 30px}}.hero{{display:block}}.state-chip{{display:inline-block;margin-top:10px}}.steps{{grid-template-columns:1fr 1fr}}.card{{padding:13px}}.mapping-top{{grid-template-columns:1fr auto}}.mapping-top select{{grid-column:1/2;min-width:0}}.values{{grid-template-columns:1fr}}.arrow{{transform:rotate(90deg);text-align:center}}.icc{{grid-template-columns:1fr}}.submit-row{{align-items:stretch;flex-direction:column}}.primary{{width:100%}}.progress-summary{{grid-template-columns:1fr auto}}.time{{grid-column:1/3}}.job-row{{grid-template-columns:minmax(0,1fr) 86px 40px}}}}
  </style>
</head>
<body>
<main class="app">
  <header class="hero">
    <div><div class="eyebrow">Dianchi · Production Tools</div><h1>AI 转 CDR</h1><p>批量上传、CMYK 精确映射、ICC 留档；mini4 逐份转换并重新打开验收。</p></div>
    <span class="state-chip" id="stateChip">等待选择文件</span>
  </header>
  <section class="steps">
    <div class="step"><strong>01 · AI 检查</strong>上传与校验原稿</div>
    <div class="step"><strong>02 · PDF 桥接</strong>Illustrator 矢量导出</div>
    <div class="step"><strong>03 · CDR 生成</strong>CorelDRAW 顺序处理</div>
    <div class="step"><strong>04 · 重新验收</strong>非空预览与留档</div>
  </section>

  <section class="card">
    <div class="card-head"><div><h2>1. 批量选择 AI</h2><div class="hint">一次最多 20 份；后台按队列顺序处理，不在同一台 mini4 上并行抢占软件窗口。</div></div></div>
    <label class="drop" id="dropZone"><input id="aiFiles" type="file" accept=".ai,application/postscript" multiple><b>点击选择或拖入 AI 文件</b><span>支持批量选择，单份不超过 100 MB，整批不超过 500 MB</span></label>
    <div class="file-list" id="fileList"></div>
  </section>

  <section class="card">
    <div class="card-head"><div><h2>2. 颜色校准（可选）</h2><div class="hint">工具自动识别原稿 CMYK；设计师只需选择原稿色块并填写目标数值。原 AI 不会被修改。</div></div></div>
    <div class="toolbar"><select id="activeFile"><option value="">请先选择 AI 文件</option></select><button class="secondary" id="copyAll" type="button">复制当前规则到全部</button><button class="secondary" id="addMapping" type="button">＋ 新增 CMYK 映射</button></div>
    <div class="mapping-list" id="mappingList"><div class="empty">未设置手动颜色映射，将按原稿颜色转换。</div></div>
    <div class="icc">
      <label class="icc-picker"><span class="secondary">载入 ICC 颜色文件…</span><input id="iccFile" type="file" accept=".icc,.icm,application/vnd.iccprofile"></label>
      <div class="icc-status" id="iccStatus">未载入 ICC；当前仅校验、保存并留档，不会未经确认自动转换颜色空间。</div>
    </div>
    <div class="notice">CMYK 规则必须在原稿中实际命中；任何规则命中 0 个对象时，该文件会明确失败，不会伪装成校准成功。</div>
    <form class="submit-row" id="batchForm" novalidate><div class="status" id="submitStatus">不填写 CMYK 或 ICC 也可以直接按原稿颜色提交。</div><button class="primary" id="submitBatch" type="submit">提交批量转换</button></form>
  </section>

  <section class="card" id="progressCard" hidden>
    <div class="card-head"><div><h2>3. 转换进度</h2><div class="hint">页面可保持打开；离开后，从小助手重新进入也能查看本批任务状态。</div></div></div>
    <div class="progress-summary"><div class="bar"><i id="overallBar"></i></div><div class="percent" id="overallPercent">0%</div><div class="time" id="timeText">已用 00:00 · 预计剩余 --:--</div></div>
    <div class="job-list" id="jobList"></div>
  </section>
  <div class="footer"><button class="secondary" id="closeView" type="button">返回飞书</button></div>
</main>
<script>
  const token={token_json};
  const base=`/api/v1/assistant-attachments/${{token}}`;
  const state={{files:[],palettes:[],mappings:[],jobs:[],downloads:{{}},batchStartedAt:0,timer:null,submitting:false}};
  const $=id=>document.getElementById(id);
  function showClientError(error){{
    state.submitting=false;
    $('submitBatch').disabled=false;
    $('submitStatus').textContent=`页面执行失败：${{error?.message||error||'请重新打开工具后再试'}}`;
    $('submitStatus').className='status bad';
  }}
  window.addEventListener('error',event=>showClientError(event.error||event.message));
  window.addEventListener('unhandledrejection',event=>showClientError(event.reason));
  const escapeHtml=value=>String(value).replace(/[&<>"']/g,char=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[char]));
  const formatBytes=size=>size>=1024*1024?`${{(size/1024/1024).toFixed(1)}} MB`:`${{Math.ceil(size/1024)}} KB`;
  const formatTime=seconds=>{{seconds=Math.max(0,Math.round(seconds||0));return `${{String(Math.floor(seconds/60)).padStart(2,'0')}}:${{String(seconds%60).padStart(2,'0')}}`;}};
  const emptyMapping=()=>({{label:'',source:{{c:'',m:'',y:'',k:''}},target:{{c:'',m:'',y:'',k:''}},apply_to:'all',tolerance:.1}});
  const colorKey=color=>['c','m','y','k'].map(key=>Number(color[key])).join('|');
  const cmykText=color=>`C ${{Number(color.c)}} · M ${{Number(color.m)}} · Y ${{Number(color.y)}} · K ${{Number(color.k)}}`;
  const cmykCss=color=>{{const c=Number(color.c)/100,m=Number(color.m)/100,y=Number(color.y)/100,k=Number(color.k)/100;return `rgb(${{Math.round(255*(1-c)*(1-k))}},${{Math.round(255*(1-m)*(1-k))}},${{Math.round(255*(1-y)*(1-k))}})`;}};
  async function extractCmykColors(file){{
    const text=await file.slice(0,4*1024*1024).text();
    const colors=[];const seen=new Set();const resource=/<rdf:li\\b[^>]*rdf:parseType=["']Resource["'][^>]*>([\\s\\S]*?)<\\/rdf:li>/gi;let match;
    const tagValue=(block,tag)=>{{const found=block.match(new RegExp(`<${{tag}}>([\\s\\S]*?)<\\/${{tag}}>`,`i`));return found?found[1].replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').trim():'';}};
    while((match=resource.exec(text))&&colors.length<64){{const block=match[1];if(tagValue(block,'xmpG:mode').toUpperCase()!=='CMYK')continue;const color={{name:tagValue(block,'xmpG:swatchName')||`原稿颜色 ${{colors.length+1}}`,c:Number(tagValue(block,'xmpG:cyan')),m:Number(tagValue(block,'xmpG:magenta')),y:Number(tagValue(block,'xmpG:yellow')),k:Number(tagValue(block,'xmpG:black'))}};if(!['c','m','y','k'].every(key=>Number.isFinite(color[key])&&color[key]>=0&&color[key]<=100))continue;const key=colorKey(color);if(seen.has(key))continue;seen.add(key);colors.push(color);}}
    return colors;
  }}

  function renderFiles(){{
    $('fileList').innerHTML=state.files.map((file,index)=>`<div class="file-row"><div><div class="file-name">${{escapeHtml(file.name)}}</div><div class="file-meta">${{formatBytes(file.size)}} · 自动识别 ${{state.palettes[index]?.length||0}} 个 CMYK 色板 · ${{state.mappings[index]?.length||0}} 条规则</div></div><button class="remove" type="button" data-remove="${{index}}">移除</button></div>`).join('');
    $('fileList').querySelectorAll('[data-remove]').forEach(button=>button.onclick=()=>{{const index=Number(button.dataset.remove);state.files.splice(index,1);state.palettes.splice(index,1);state.mappings.splice(index,1);renderFiles();renderFileSelector();renderMappings();}});
    $('stateChip').textContent=state.files.length?`已选择 ${{state.files.length}} 份 AI`:'等待选择文件';
  }}
  function renderFileSelector(){{
    const previous=Number($('activeFile').value)||0;
    $('activeFile').innerHTML=state.files.length?state.files.map((file,index)=>`<option value="${{index}}">${{index+1}}. ${{escapeHtml(file.name)}}</option>`).join(''):'<option value="">请先选择 AI 文件</option>';
    if(state.files.length)$('activeFile').value=String(Math.min(previous,state.files.length-1));
  }}
  async function setFiles(fileList){{
    const incoming=[...fileList].filter(file=>file.name.toLowerCase().endsWith('.ai'));
    if(!incoming.length)return;
    const combined=[...state.files,...incoming].slice(0,20);
    const oldCount=state.files.length;state.files=combined;
    for(let index=oldCount;index<combined.length;index++){{state.mappings[index]=[];state.palettes[index]=await extractCmykColors(combined[index]);}}
    renderFiles();renderFileSelector();renderMappings();
  }}
  $('aiFiles').addEventListener('change',event=>{{setFiles(event.target.files).catch(showClientError);event.target.value='';}});
  for(const eventName of ['dragenter','dragover'])$('dropZone').addEventListener(eventName,event=>{{event.preventDefault();$('dropZone').classList.add('drag');}});
  for(const eventName of ['dragleave','drop'])$('dropZone').addEventListener(eventName,event=>{{event.preventDefault();$('dropZone').classList.remove('drag');if(eventName==='drop')setFiles(event.dataTransfer.files).catch(showClientError);}});

  function renderMappings(){{
    const fileIndex=Number($('activeFile').value);
    const mappings=Number.isInteger(fileIndex)&&state.mappings[fileIndex]?state.mappings[fileIndex]:[];
    if(!mappings.length){{$('mappingList').innerHTML='<div class="empty">未设置手动颜色映射，将按原稿颜色转换。</div>';renderFiles();return;}}
    const components=['c','m','y','k'];
    const palette=state.palettes[fileIndex]||[];
    $('mappingList').innerHTML=mappings.map((mapping,index)=>{{const currentKey=components.every(key=>mapping.source[key]!=='' )?colorKey(mapping.source):'';const currentIndex=palette.findIndex(color=>colorKey(color)===currentKey);const currentColor=currentIndex>=0?palette[currentIndex]:mapping.source;const options=palette.map((color,colorIndex)=>`<option value="${{colorIndex}}" ${{colorIndex===currentIndex?'selected':''}}>${{escapeHtml(color.name)}} · ${{cmykText(color)}}</option>`).join('');const copiedOption=currentKey&&currentIndex<0?`<option value="current" selected>已复制的原稿色 · ${{cmykText(mapping.source)}}</option>`:'';return `<div class="mapping" data-map="${{index}}"><div class="mapping-top"><input class="field" data-key="label" placeholder="颜色名称，例如：企业蓝" value="${{escapeHtml(mapping.label)}}"><select data-key="apply_to"><option value="all" ${{mapping.apply_to==='all'?'selected':''}}>填充和描边</option><option value="fill" ${{mapping.apply_to==='fill'?'selected':''}}>仅填充</option><option value="stroke" ${{mapping.apply_to==='stroke'?'selected':''}}>仅描边</option></select><button class="delete" data-delete="${{index}}" type="button">删除</button></div><div class="values"><div class="color-block"><span>原稿颜色（自动识别，只读）</span><div class="source-picker"><i class="swatch" style="--swatch:${{currentKey?cmykCss(currentColor):'#fff'}}"></i><select data-source-select><option value="">选择 AI 中的颜色</option>${{copiedOption}}${{options}}</select><div class="source-values">${{currentKey?cmykText(mapping.source):'无需记忆数值，直接选择色板'}}</div></div></div><span class="arrow">→</span><div class="color-block"><span>目标 CMYK（设计师填写）</span><div class="cmyk">${{components.map(key=>`<label>${{key.toUpperCase()}}<input inputmode="decimal" data-group="target" data-component="${{key}}" placeholder="0" value="${{escapeHtml(mapping.target[key])}}"></label>`).join('')}}</div></div></div></div>`;}}).join('');
    $('mappingList').querySelectorAll('.mapping').forEach(row=>{{const index=Number(row.dataset.map);row.querySelector('[data-key="label"]').oninput=event=>mappings[index].label=event.target.value;row.querySelector('[data-key="apply_to"]').onchange=event=>mappings[index].apply_to=event.target.value;row.querySelector('[data-source-select]').onchange=event=>{{if(event.target.value==='current')return;if(event.target.value===''){{mappings[index].source={{c:'',m:'',y:'',k:''}};renderMappings();return;}}const selected=palette[Number(event.target.value)];if(!selected)return;mappings[index].source={{c:selected.c,m:selected.m,y:selected.y,k:selected.k}};if(!mappings[index].label.trim())mappings[index].label=selected.name;renderMappings();}};row.querySelectorAll('[data-group="target"]').forEach(input=>input.oninput=event=>mappings[index].target[event.target.dataset.component]=event.target.value);}});
    $('mappingList').querySelectorAll('[data-delete]').forEach(button=>button.onclick=()=>{{mappings.splice(Number(button.dataset.delete),1);renderMappings();}});renderFiles();
  }}
  $('activeFile').addEventListener('change',renderMappings);
  $('addMapping').addEventListener('click',()=>{{const index=Number($('activeFile').value);if(!state.files[index]){{$('submitStatus').textContent='请先选择 AI 文件。';$('submitStatus').className='status bad';return;}}if(!state.palettes[index]?.length){{$('submitStatus').textContent='这份 AI 没有可读取的 CMYK 色板；请在 Illustrator 保存时启用 PDF 兼容，或直接按原稿转换。';$('submitStatus').className='status bad';return;}}if(state.mappings[index].length>=64)return;state.mappings[index].push(emptyMapping());renderMappings();}});
  $('copyAll').addEventListener('click',()=>{{const index=Number($('activeFile').value);if(!state.files[index])return;const source=state.mappings[index]||[];state.mappings=state.files.map(()=>JSON.parse(JSON.stringify(source)));renderFiles();$('submitStatus').textContent=`已把当前 ${{source.length}} 条规则复制到全部文件。`;$('submitStatus').className='status ok';}});
  $('iccFile').addEventListener('change',event=>{{const file=event.target.files[0];$('iccStatus').textContent=file?`已选择：${{file.name}} · ${{formatBytes(file.size)}}；服务器会再次校验 ICC 标准签名。`:'未载入 ICC；当前仅校验、保存并留档。';}});

  function validatedMappings(){{
    return state.mappings.map((mappings,fileIndex)=>mappings.map((mapping,mapIndex)=>{{const normalized={{label:mapping.label.trim()||`颜色映射 ${{mapIndex+1}}`,source:{{}},target:{{}},apply_to:mapping.apply_to,tolerance:.1}};for(const group of ['source','target'])for(const key of ['c','m','y','k']){{const value=Number(mapping[group][key]);if(mapping[group][key]===''||!Number.isFinite(value)||value<0||value>100)throw new Error(`${{state.files[fileIndex].name}} 第 ${{mapIndex+1}} 条映射的 CMYK 必须全部是 0–100 的数字。`);normalized[group][key]=value;}}return normalized;}}));
  }}
  function uploadBatch(form){{
    return new Promise((resolve,reject)=>{{const xhr=new XMLHttpRequest();xhr.open('POST',`${{base}}/ai-cdr/jobs`);xhr.responseType='json';xhr.upload.onprogress=event=>{{if(event.lengthComputable){{const percent=Math.max(1,Math.round(event.loaded/event.total*8));$('overallBar').style.width=`${{percent}}%`;$('overallPercent').textContent=`${{percent}}%`;$('submitStatus').textContent=`正在上传到 NAS：${{Math.round(event.loaded/event.total*100)}}%`;}}}};xhr.onload=()=>xhr.status>=200&&xhr.status<300?resolve(xhr.response):reject(new Error(xhr.response?.detail||'上传失败'));xhr.onerror=()=>reject(new Error('网络中断，上传失败'));xhr.send(form);}});
  }}
  async function submitBatch(event){{
    if(event)event.preventDefault();
    if(state.submitting)return;
    if(!state.files.length){{$('submitStatus').textContent='请先选择至少一份 AI 文件。';$('submitStatus').className='status bad';return;}}
    const total=state.files.reduce((sum,file)=>sum+file.size,0);if(state.files.some(file=>file.size>100*1024*1024)||total>500*1024*1024){{$('submitStatus').textContent='文件超过单份 100 MB 或整批 500 MB 限制。';$('submitStatus').className='status bad';return;}}
    let mappings;try{{mappings=validatedMappings();}}catch(error){{$('submitStatus').textContent=error.message;$('submitStatus').className='status bad';return;}}
    const form=new FormData();state.files.forEach(file=>form.append('files',file,file.name));form.append('calibration',JSON.stringify({{files:state.files.map((file,index)=>({{name:file.name,mappings:mappings[index]}}))}}));const icc=$('iccFile').files[0];if(icc)form.append('icc_profile',icc,icc.name);
    state.submitting=true;$('submitBatch').disabled=true;$('submitStatus').textContent='正在上传 AI 与颜色配置…';$('submitStatus').className='status';$('progressCard').hidden=false;state.batchStartedAt=Date.now();
    try{{const payload=await uploadBatch(form);state.jobs=payload.jobs||[];$('submitStatus').textContent=`已提交 ${{state.jobs.length}} 份 AI，mini4 将按队列顺序处理。`;$('submitStatus').className='status ok';$('stateChip').textContent='批量转换进行中';renderJobs();if(state.timer)clearInterval(state.timer);state.timer=setInterval(refreshJobs,2000);await refreshJobs();}}
    catch(error){{state.submitting=false;$('submitStatus').textContent=error.message||'提交失败';$('submitStatus').className='status bad';$('submitBatch').disabled=false;}}
  }}
  $('batchForm').addEventListener('submit',submitBatch);
  $('submitBatch').addEventListener('pointerup',submitBatch);
  $('submitBatch').addEventListener('touchend',submitBatch,{{passive:false}});
  function renderJobs(){{
    $('jobList').innerHTML=state.jobs.map((job,index)=>{{const download=state.downloads[index]||{{}};const busy=download.status==='downloading';const resultClass=download.status==='saved'?'ok':download.status==='failed'?'bad':'';return `<div class="job-row"><div><div class="file-name">${{escapeHtml(job.source_name)}}</div><div class="job-state ${{job.status||''}}">${{escapeHtml(job.error?`${{job.message||'转换失败'}}：${{job.error}}`:(job.message||'排队等待 mini4'))}}${{job.download_url?`<button class="download" type="button" data-download-index="${{index}}" ${{busy?'disabled':''}}>${{busy?'下载中…':'下载 CDR'}}</button>`:''}}${{download.message?`<span class="download-result ${{resultClass}}">${{escapeHtml(download.message)}}</span>`:''}}</div></div><div class="bar"><i style="width:${{Math.max(0,Math.min(100,job.percentage||0))}}%"></i></div><div class="job-percent">${{Math.round(job.percentage||0)}}%</div></div>`;}}).join('');
    const overall=state.jobs.length?state.jobs.reduce((sum,job)=>sum+(job.percentage||0),0)/state.jobs.length:0;$('overallBar').style.width=`${{overall}}%`;$('overallPercent').textContent=`${{Math.round(overall)}}%`;
    const elapsed=(Date.now()-state.batchStartedAt)/1000;const completed=state.jobs.filter(job=>['succeeded','failed'].includes(job.status));let remaining='--:--';if(completed.length&&completed.length<state.jobs.length){{const average=completed.reduce((sum,job)=>sum+(job.elapsed_seconds||elapsed/completed.length),0)/completed.length;remaining=formatTime(average*(state.jobs.length-completed.length));}}if(completed.length===state.jobs.length)remaining='00:00';$('timeText').textContent=`已用 ${{formatTime(elapsed)}} · 预计剩余 ${{remaining}}`;
    if(state.jobs.length&&completed.length===state.jobs.length){{clearInterval(state.timer);state.timer=null;state.submitting=false;const failures=state.jobs.filter(job=>job.status==='failed').length;$('stateChip').textContent=failures?`完成，${{failures}} 份失败`:'全部转换完成';$('stateChip').style.color=failures?'var(--red)':'var(--green)';$('submitBatch').disabled=false;}}
  }}
  $('jobList').addEventListener('click',async event=>{{
    const button=event.target.closest('[data-download-index]');if(!button)return;
    const index=Number(button.dataset.downloadIndex);const job=state.jobs[index];if(!job?.download_url)return;
    const suggestedName=job.download_name||`${{job.source_name.replace(/\\.ai$/i,'')}}.cdr`;
    state.downloads[index]={{status:'downloading',message:`正在下载：${{suggestedName}}`}};renderJobs();
    try{{
      const response=await fetch(job.download_url,{{cache:'no-store'}});if(!response.ok){{let message='CDR 下载失败';try{{const payload=await response.json();message=payload.detail||message;}}catch(error){{}}throw new Error(message);}}
      const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download=suggestedName;link.hidden=true;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
      state.downloads[index]={{status:'saved',message:`下载已开始｜位置：电脑“下载”文件夹｜文件名：${{suggestedName}}`}};renderJobs();
    }}catch(error){{state.downloads[index]={{status:'failed',message:`下载失败：${{error?.message||'请重试'}}`}};renderJobs();}}
  }});
  async function refreshJobs(){{try{{const response=await fetch(`${{base}}/ai-cdr/jobs`,{{cache:'no-store'}});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'读取进度失败');if(payload.jobs?.length){{state.jobs=payload.jobs;if(!state.batchStartedAt)state.batchStartedAt=Date.parse(state.jobs[0].submitted_at)||Date.now();}}renderJobs();}}catch(error){{$('stateChip').textContent='进度连接暂时中断';}}}}
  $('closeView').addEventListener('click',()=>{{try{{window.LarkAPI?.webview?.close?.();}}catch(error){{}}history.back();}});
  refreshJobs().then(()=>{{if(state.jobs.length){{state.batchStartedAt=Date.parse(state.jobs[0].submitted_at)||Date.now();$('progressCard').hidden=false;renderJobs();state.timer=setInterval(refreshJobs,2000);}}}});
</script>
</body>
</html>"""


__all__ = ["build_ai_cdr_page"]
