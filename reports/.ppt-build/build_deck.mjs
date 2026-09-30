import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {FileBlob,Presentation,PresentationFile} from '@oai/artifact-tool';
const req=createRequire('C:/Users/a1310/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/package.json');
req('skia-canvas').FontLibrary.use({'等线 Light':'C:/Windows/Fonts/Dengl.ttf'});
const ROOT='D:/数据汇总/driving_defect_agent/reports/ppt';
const BUILD=path.dirname(ROOT)+'/.ppt-build';
const SOURCE='C:/Users/a1310/Downloads/城市交通研究院PPT模板.pptx';
const DATA='D:/数据汇总/driving_defect_agent/docs/当前数据概况与结构说明.md';
const WORK='D:/数据汇总/driving_defect_agent/docs/当前Agent详细工作流程.md';
const EVAL='D:/数据汇总/driving_defect_agent/reports/Agent工作效果对比评价_20260927-113112-432900-live.md';
const C={blue:'#00519C',green:'#89C53B',ink:'#202B33',gray:'#5B5C5E',light:'#F1F4F6',line:'#D4DCE2',pale:'#EAF2F8',palegreen:'#F1F7E8',red:'#A8493E',white:'#FFFFFF'};
await fs.mkdir(BUILD+'/render',{recursive:true});
const imported=await PresentationFile.importPptx(await FileBlob.load(SOURCE));
// Preserve the imported masters, layouts and original branded images. Remove obsolete dates in the copy.
const p=imported;
for(const layout of [...p.masters.items,...p.layouts.items])for(const sh of layout.shapes.items){if(sh.text?.toString().includes('2023/10/25'))sh.text.replace('2023/10/25','');}
const original=[...p.slides.items];
const base=original[3];
const slides=Array.from({length:18},()=>base.duplicate());
original.forEach(s=>s.delete());
slides.forEach((s,i)=>s.moveTo(i));
const authored=[];
function tx(s,text,x,y,w,h,size=28,color=C.ink,bold=false,align='left',opts={}){
 const sh=s.shapes.add({geometry:'textbox',name:opts.name??text.replace(/\n/g,' ').slice(0,45),position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
 sh.text=text;
 sh.text.style={typeface:opts.font??'等线',fontSize:size,color,bold,alignment:align,verticalAlignment:opts.middle?'middle':'top',autoFit:'none',wrap:'square',insets:{top:0,right:0,bottom:0,left:0},...opts.style};
 return sh;
}
function line(s,x,y,w,color=C.line,width=1){return s.shapes.add({geometry:'line',name:'分隔线',position:{left:x,top:y,width:w,height:0},fill:'none',line:{fill:color,width}});}
function node(s,text,x,y,w,h=66,kind='program',size=27){
 const fill=kind==='model'?C.pale:kind==='output'?C.palegreen:C.light;
 const sh=s.shapes.add({geometry:'rect',name:text.replace(/\n/g,' '),position:{left:x,top:y,width:w,height:h},fill,line:{fill:kind==='model'?C.blue:kind==='output'?C.green:C.line,width:1.5}});
 sh.text=text;sh.text.style={typeface:'等线',fontSize:size,color:C.ink,alignment:'center',verticalAlignment:'middle',autoFit:'none',wrap:'square',insets:{top:8,right:9,bottom:8,left:9}};return sh;
}
function arrow(s,a,b,from='right',to='left',dashed=false){return s.shapes.connect(a,b,{kind:from==='right'&&to==='left'?'straight':'elbow',fromSide:from,toSide:to,line:{fill:C.gray,width:1.6,style:dashed?'dashed':'solid'},tail:{type:'triangle',width:'sm',length:'sm'}});}
function foot(s,text){tx(s,text,88,620,1104,44,24,C.gray);}
function heading(s,num,title){
 tx(s,title,88,75.42,1104,102.08,58.6667,'#000000',false,'left',{font:'等线 Light',middle:true,name:'页面标题'});
 tx(s,String(num).padStart(2,'0'),1137,673,52,26,21,C.gray,false,'right',{name:'页码'});
}
function label(s,text,x,y,w=500){return tx(s,text,x,y,w,37,31,C.blue,true);}
function para(s,text,x,y,w,h=80,size=28){return tx(s,text,x,y,w,h,size);}
function table(s,values,x,y,w,h,widths,size=24){
 const t=s.tables.add({rows:values.length,columns:values[0].length,left:x,top:y,width:w,height:h,values,columnWidths:widths});
 t.borders.assign({fill:C.line,width:0.8});
 for(let r=0;r<values.length;r++){
  t.rows[r].height=h/values.length;
  for(let c=0;c<values[r].length;c++){
   const cell=t.getCell(r,c);cell.fill=r===0?C.blue:r%2===0?C.light:C.white;
   cell.text.style={typeface:'等线',fontSize:size,color:r===0?C.white:C.ink,bold:r===0,verticalAlignment:'middle',insets:{left:12,right:10,top:5,bottom:5}};
  }
 }
 return t;
}
async function pic(s,file,x,y,w,h,alt){return s.images.add({blob:new Uint8Array(await fs.readFile(file)),contentType:file.toLowerCase().endsWith('.png')?'image/png':'image/jpeg',alt,fit:'contain',position:{left:x,top:y,width:w,height:h}});}
function notes(s,num,text,extra=''){
 text=(Array.isArray(text)?text.filter(t=>!String(t).includes('original_result')).join('\n\n'):text);
 s.speakerNotes.textFrame.setText(text+'\n\n'+extra);
 authored.push({slide:num,title:s.shapes.items.find(x=>x.name==='页面标题')?.text?.toString(),notes:text+'\n'+extra});
}
const content=JSON.parse((await fs.readFile(BUILD+'/content_data_workflow.json','utf8')).replace(/^\uFEFF/,''));
const noteMap=new Map(content.slides.map(x=>[x.slide_number,x.notes]));
let ev;
try{ev=JSON.parse((await fs.readFile(BUILD+'/content_evaluation.json','utf8')).replace(/^\uFEFF/,''));for(const o of ev.slides??[])noteMap.set(o.slide_number??o.slide,o.notes??'');}catch{}
function nn(s,n,extra=''){notes(s,n,noteMap.get(n)||'来源：'+EVAL,extra);}

// 1. Scale: one primary number group, a compact native evidence table.
{
 const s=slides[0];heading(s,1,'数据规模与样本构成');
 tx(s,'10',88,192,190,99,88,C.blue,true);tx(s,'个上传视频',270,237,270,47,36);
 tx(s,'12分42秒',88,322,440,76,61,C.blue);tx(s,'原视频总时长约 761.972 秒',88,402,430,38,26,C.gray);
 para(s,'6 个横屏包，4 个竖屏包\n素材包含解说、截图、回放与慢放',88,483,455,98,28);
 table(s,[['输入材料','数量'],['预切视频片段','260 个'],['全画面采样图','1,417 张'],['字幕裁剪图（仅 v1）','111 张'],['默认 ASR','212 条'],['公开评论样本','427 条']],572,207,620,350,[405,215],27);
 foot(s,'全部为 demo_only 示例输入；10 个视频包不等于 10 起已确认、相互独立的事故。');nn(s,1);
}
// 2. Package layers plus the actual OCR processing counts.
{
 const s=slides[1];heading(s,2,'单个数据包的结构');
 tx(s,'统一入口：agent_input.json',88,188,1104,39,29,C.gray);
 const a=node(s,'来源材料',88,245,318,64),b=node(s,'派生输入',481,245,318,64),c=node(s,'索引与溯源',874,245,318,64);
 arrow(s,a,b);arrow(s,b,c);
 para(s,'上传原视频与音轨\n标题、简介与评论',103,328,300,82,28);
 para(s,'短片段与采样帧\nASR、OCR 和 spans',496,328,300,82,28);
 para(s,'跨模态引用与校验清单\n处理依据和待复核事项',889,328,303,82,28);
 line(s,88,435,1104);
 label(s,'OCR 的三个处理层',88,453);
 const d=node(s,'15,820\n原始检测',88,507,300,86,'program',28),e=node(s,'7,343\n默认点观察',490,507,300,86,'model',28),f=node(s,'2,075\n合并 spans',892,507,300,86,'output',28);
 arrow(s,d,e);arrow(s,e,f);foot(s,'三层 OCR 来自同一批材料，不可相加为独立证据数量。');nn(s,2);
}
// 3. Editable conceptual time alignment, explicitly labeled as an illustration.
{
 const s=slides[2];heading(s,3,'时间与跨模态关联');
 tx(s,'上传视频时间轴（关联示意）',88,190,690,42,30,C.blue,true);
 const xs=[255,465,675,885];line(s,255,260,630,C.gray,2);
 ['0 秒','3 秒','6 秒','9 秒'].forEach((v,i)=>{line(s,xs[i],252,0,C.gray,1);tx(s,v,xs[i]-30,221,85,35,24,C.gray,false,'center');});
 tx(s,'视频片段',88,290,150,38,27);node(s,'[start, end)',255,284,405,47,'program',25);node(s,'下一片段',678,284,207,47,'program',25);
 tx(s,'采样帧',88,356,150,38,27);[255,465,675,800].forEach((x,i)=>node(s,'帧 '+String(i+1),x,350,85,45,'model',23));
 tx(s,'ASR',88,422,150,38,27);node(s,'同一句语音可跨片段引用',340,415,505,49,'program',26);
 tx(s,'OCR',88,489,150,38,27);node(s,'span',255,481,135,52,'output',25);const sp=node(s,'底层 OCR ID',469,481,189,52,'program',25),fr=node(s,'帧 ID',738,481,147,52,'model',25);arrow(s,s.shapes.items.find(x=>x.name==='span'),sp);arrow(s,sp,fr);
 para(s,'统一使用\n上传视频秒数\n保留原 ID\n避免重复计数',951,277,241,169,27);
 para(s,'427 条评论\n均无视频内时间\n仅作视频级背景',951,463,241,123,27);
 foot(s,'上传视频时间、画面时钟和真实事件时间应分别理解。');nn(s,3,'图中 0、3、6、9 秒与帧序号是关联原理示意，不代表某条真实 ASR 或 OCR 的起止位置。');
}
// 4. Structural QA versus semantic review.
{
 const s=slides[3];heading(s,4,'数据质量与使用边界');
 [['10/10','数据包可加载'],['0','error'],['11','warning'],['5','info']].forEach(([v,t],i)=>{tx(s,v,88+i*286,189,235,72,60,i===0?C.blue:C.ink,true);tx(s,t,88+i*286,269,235,39,26,C.gray);});
 table(s,[['当前状态','使用影响'],['11 条回复缺直接父评论','上下文不完整，保留原父 ID'],['v3、v7 默认 ASR 为空','音轨仍存在，必要时人工复听'],['212 条 ASR、7,343 条 OCR 未人工复核','可用于线索分析，不能作为人工金标准'],['缺少随包缺陷与因果真值','当前适合流程研究，完整评价需补充标注']],88,333,1104,253,[570,534],26);
 foot(s,'结构检查通过，仅说明计数、引用、路径与时间等条件满足要求。');nn(s,4);
}
// 5. Ordered pipeline. Blue nodes are five model roles, not simultaneous agents.
{
 const s=slides[4];heading(s,5,'Agent 总体架构与五个角色');
 tx(s,'Python 协调器控制顺序、状态、预算与结果落盘',88,190,1104,40,31,C.blue,true);
 const top=['适配与校验','文本提取\n来源陈述','独立视觉观察\n画面现象','生成候选事件'];
 const a=top.map((t,i)=>node(s,t,88+i*286,259,246,88,[1,2].includes(i)?'model':'program',28));
 for(let i=0;i<3;i++)arrow(s,a[i],a[i+1]);
 const low=['规则分级与报告','候选归因\n待验证解释','审查与有限补证\n冲突和缺失','RAG 检索与对齐\n证据关系'];
 const b=low.map((t,i)=>node(s,t,88+i*286,409,246,88,i>0?'model':'output',28));
 arrow(s,a[3],b[3],'bottom','top');for(let i=3;i>0;i--)arrow(s,b[i],b[i-1],'left','right');
 para(s,'蓝色节点为五个模型角色，可使用同一模型。\n首轮视觉不接收文本结论；本地检索、规则校验和状态管理贯穿全程。',88,537,1104,76,28);
 nn(s,5);
}
// 6. Two evidence routes, preserving execution order and visual independence.
{
 const s=slides[5];heading(s,6,'文本提取与候选事件');
 label(s,'先提取来源陈述',88,198,450);label(s,'再进行独立视觉观察',660,198,532);
 para(s,'标题、简介、ASR、OCR spans、评论',88,255,494,70,28);
 const a=node(s,'Claim\n谁说了什么、引用与时间',88,343,494,104,'model',30);
 const b=node(s,'VisualObservation\n可见现象、帧 ID 与局限',660,261,532,88,'model',28);
 const c=node(s,'程序合并观察簇',660,385,532,62,'program',28);arrow(s,b,c,'bottom','top');
 const d=node(s,'候选 Event',660,484,532,58,'output',30);arrow(s,c,d,'bottom','top');
 para(s,'视觉请求仅接收图片、帧 ID、\n时间戳与窗口范围。',88,479,494,75,28);
 foot(s,'当前默认：6 秒核心窗口，前后各 1 秒；约 1 Hz，每请求最多 8 张图。');
 nn(s,6,'合并条件：同类现象、时间相隔不超过 1 秒，且没有剪辑或镜头切换疑点。图的左右分区表示两类证据，不表示并行运行。');
}
// 7. Editable retrieval/relationship flow.
{
 const s=slides[6];heading(s,7,'RAG 检索与证据对齐');
 const a=node(s,'有时间的陈述',88,219,285,66,'model',29),b=node(s,'无时间的陈述',88,352,285,66,'model',29);
 const c=node(s,'时间重叠\n或邻近 2 秒',437,210,300,85,'program',28),d=node(s,'语义 top-5\n与 BM25 top-5 的并集',437,339,300,92,'program',28);
 arrow(s,a,c);arrow(s,b,d);
 const e=node(s,'单条陈述\n与候选事件对齐',825,276,367,99,'model',30);arrow(s,c,e);arrow(s,d,e);
 table(s,[['关系','判定用途'],['支持 / 反对','判断陈述与事件是否相符或冲突'],['可能相关 / 无关','保留候选关系或排除关联']],88,475,1104,124,[330,774],26);
 foot(s,'在本地已导入证据中检索；当前最多 2 个对齐 worker，检索相似度不等于支持强度。');nn(s,7);
}
// 8. Review/limited evidence acquisition.
{
 const s=slides[7];heading(s,8,'事件审查与有限补证');
 const a=node(s,'完整事件证据包',88,218,280,76,'program',29),b=node(s,'审查角色\n冲突、缺失、替代解释',435,208,369,97,'model',28);arrow(s,a,b);
 const c=node(s,'归因用事件证据包\n进入候选归因',882,218,310,76,'output',27);arrow(s,b,c);tx(s,'无需补帧',807,181,121,30,21,C.gray);
 const d=node(s,'自动补证：局部抽帧与重新观察',435,371,550,69,'model',28);arrow(s,b,d,'bottom','top');arrow(s,d,c,'right','bottom');
 para(s,'每个事件最多 1 轮\n4 Hz、最长 6 秒、最多 24 张图',452,461,727,77,30);
 const e=node(s,'人工复核队列',88,371,280,69,'program',28);arrow(s,b,e,'bottom','top');tx(s,'需补帧',748,337,158,30,23,C.gray);
 para(s,'音频、系统状态、\n来源身份等请求',88,470,295,80,27);
 foot(s,'补证不会重跑全局事件合并与对齐；补充观察仍须在最终摘要中正确归并。');nn(s,8);
}
// 9. Hypothesis format and conservative grading.
{
 const s=slides[8];heading(s,9,'候选归因与证据分级');
 label(s,'每事件最多 3 个候选解释',88,194,670);
 tx(s,'证据不足时可弃答',805,197,387,36,28,C.gray,false,'right');
 table(s,[['假设要素','需要保留的信息'],['解释依据','支持证据、反对证据与替代解释'],['反事实条件','改变什么、保持什么、预期有何差异'],['验证需求','缺失信息、所需数据与后续验证条件']],88,252,1104,213,[283,821],28);
 const a=node(s,'现象证据',88,510,310,66,'program',29),b=node(s,'系统状态证据',485,510,310,66,'program',29),c=node(s,'归因证据',882,510,310,66,'program',29);
 foot(s,'三个维度分别分级；所有假设均为 unverified，所有事件保留人工复核要求。');nn(s,9);
}
// 10. Runtime recovery and editable output table.
{
 const s=slides[9];heading(s,10,'运行控制与结果产物');
 const a=node(s,'输出与引用校验',88,204,319,70,'program',29),b=node(s,'格式修复\n失败后最多追加 3 轮',481,196,319,86,'program',27),c=node(s,'缓存、检查点与恢复',874,204,318,70,'program',27);arrow(s,a,b);arrow(s,b,c);
 table(s,[['产物','研究者可以查看什么'],['claims / observations','来源陈述与画面观察'],['events / hypotheses','事件、候选解释及限制条件'],['report / evidence_graph','分析摘要、支持与冲突关系'],['review_queue / summary','待复核事项与批量运行概况']],88,329,1104,251,[422,682],27);
 foot(s,'complete 表示处理流程完成；内容正确性仍需结合证据和人工参考判断。');nn(s,10);
}
// 11. Evaluation setup and output volume.
{
 const s=slides[10];heading(s,11,'评价设计与运行结果');
 tx(s,'10',88,188,156,94,80,C.blue,true);tx(s,'个视频',245,229,184,48,36);
 tx(s,'35',535,188,166,94,80,C.blue,true);tx(s,'条重点片段参考标注',703,227,489,55,34);
 para(s,'以最终事件为主进行语义对照，\n时间相交仅用于定位候选。',88,315,478,93,31);
 para(s,'单次 live 运行，全部处理完成。\n文本与视觉模型均为 qwen3.8-flash。',88,458,478,97,29);
 table(s,[['结构化产物','数量'],['最终事件记录','254'],['来源陈述','1,141'],['画面观察','1,215'],['归因假设','724']],613,315,579,259,[382,197],28);
 foot(s,'参考标注覆盖重点片段，尚未独立双人复核；不据此计算整体 Precision、Recall 或 F1。');nn(s,11,'运行批次：20260927-113112-432900-live。评价日期2026-09-27。254条为细粒度记录，不等于254个独立缺陷；724条为未验证假设。');
}
// 12. Native editable bar chart with literal source values and embedded workbook later.
{
 const s=slides[11];heading(s,12,'参考条目一致性分布');
 const ch=s.charts.add('bar',{position:{left:88,top:210,width:700,height:380},categories:['明确冲突','证据不足','未覆盖','部分一致','核心事实一致'],series:[{name:'参考条目数',values:[1,1,3,16,14],fill:C.blue,points:[{idx:0,fill:C.red},{idx:1,fill:'#BEC6CB'},{idx:2,fill:'#9DA9B0'},{idx:3,fill:C.green},{idx:4,fill:C.blue}]}],barOptions:{direction:'bar',grouping:'clustered',gapWidth:72,varyColors:true},hasLegend:false,titlePlacement:'none',chartFill:C.white,chartLine:{fill:'none',width:0},plotAreaLine:{fill:'none',width:0},xAxis:{visible:true,textStyle:{typeface:'等线',fontSize:26,fill:C.ink},majorGridlines:null,line:{fill:'none',width:0}},yAxis:{visible:true,min:0,max:18,majorUnit:3,textStyle:{typeface:'等线',fontSize:24,fill:C.gray},majorGridlines:null,line:{fill:'none',width:0}},dataLabels:{showValue:true,position:'outEnd',textStyle:{typeface:'等线',fontSize:29,bold:true,fill:C.ink}}});
 tx(s,'40.0%',865,220,327,74,62,C.blue,true);tx(s,'14 / 35 核心事实一致',865,300,327,41,27);
 tx(s,'45.7%',865,365,327,67,54,C.gray);tx(s,'16 / 35 部分一致',865,438,327,41,27);
 tx(s,'合计 85.7%',865,516,327,56,39,C.blue,true);tx(s,'核心或部分一致',865,573,327,33,25);
 foot(s,'85.7% 是条目一致性分布中的合计比例，不是事件识别准确率。');nn(s,12,'条目数合计35。14/35=40.0%，16/35=45.7%，3/35=8.6%，1/35=2.9%各两类。百分比一位小数舍入后合计100.1%，图以整数条数为主。');
}
// 13. Native comparison table. Keep all 10 videos.
{
 const s=slides[12];heading(s,13,'逐视频效果比较');
 tx(s,'每个视频单独判断，避免标注较细的视频主导整体印象',88,185,1104,40,29,C.gray);
 const rows=[['视频','整体判断','核心 / 部分 / 其他','主要结果与差异'],['v1','部分一致','4 / 1 / 2','风险接近与疑似擦碰尚未覆盖'],['v2','部分一致','0 / 1 / 0','主线有对应，握盘与离盘描述存在差异'],['v3','部分一致','0 / 1 / 0','减速通过有对应，控制主体仍需澄清'],['v4','证据不足','0 / 0 / 1','屏摄与摘要截断限制最终确认'],['v5','核心事实一致','1 / 0 / 0','接近、碰撞与静态后果分段提取'],['v6','部分一致','0 / 1 / 0','多视角线索充分，车辆运动关系有偏差'],['v7','核心事实一致','1 / 0 / 0','保留异常诉求，实际制动状态未确认'],['v8','部分一致','2 / 3 / 2','存在截图来源错置、口播遗漏'],['v9','部分一致','4 / 3 / 0','碰撞与素材边界有对应，细阶段待完善'],['v10','部分一致','2 / 6 / 0','侧翻货车线索明确，确定性表达待统一']];
 const t=table(s,rows,88,243,1104,347,[75,195,239,595],24);
 t.cells.block({row:0,column:0,rowCount:11,columnCount:4}).assign({margins:{top:0,bottom:0,left:10,right:10}});
 for(let r=0;r<11;r++)for(let c=0;c<4;c++)t.getCell(r,c).text.insets={left:10,right:10,top:0,bottom:0};
 [5,7].forEach(r=>{t.getCell(r,1).text.color=C.blue;t.getCell(r,1).text.bold=true;});
 tx(s,'视频层面：2 个核心事实一致、7 个部分一致、1 个证据不足。其他含未覆盖、不足和冲突。',88,645,1104,33,23,C.gray);nn(s,13,'为保证投影可读性，正文将未覆盖、证据不足、明确冲突合为“其他”列，三类含义并未合并。原始五类计数：v1 4/1/2/0/0；v2 0/1/0/0/0；v3 0/1/0/0/0；v4 0/0/0/1/0；v5 1/0/0/0/0；v6 0/1/0/0/0；v7 1/0/0/0/0；v8 2/3/1/0/1；v9 4/3/0/0/0；v10 2/6/0/0/0。');
}
// 14. Two sourced horizontal images, full frames preserved.
{
 const s=slides[13];heading(s,14,'典型成效：事件阶段提取');
 label(s,'v1 退出停车与环视检查',88,190,532);label(s,'v5 接近、碰撞与静态后果',660,190,532);
 await pic(s,'D:/数据汇总/数据汇总/v1/frames/frame_0091.jpg',88,239,532,222,'v1原始帧，上传视频91秒');
 await pic(s,'D:/数据汇总/数据汇总/v5/frames/frame_0022.jpg',660,239,532,222,'v5原始帧，上传视频22秒；图示接近阶段，不据此确认接触帧');
 para(s,'E027：91–93 秒，静止及退出文字\nE028：94–102 秒，操作环视并检查',88,487,532,91,27);
 para(s,'E011：21–22.5 秒，接近\nE012 / E013：碰撞线索及事后后果',660,487,532,91,27);
 foot(s,'阶段表达便于回查证据；实际辅助驾驶状态与精确接触时刻仍保留限制。');nn(s,14,'原图：D:/数据汇总/数据汇总/v1/frames/frame_0091.jpg；D:/数据汇总/数据汇总/v5/frames/frame_0022.jpg。来源报告§4.1–4.2。v1刹车动作按字幕陈述理解，不推算接管延迟。v5单帧展示接近，不声称由该帧直接证实碰撞接触。');
}
// 15. Vertical v7 frame plus two v9 frames for material boundaries.
{
 const s=slides[14];heading(s,15,'典型成效：诉求与素材区分');
 label(s,'v7 保存异常诉求',88,190,489);label(s,'v9 区分事故影像与后续素材',650,190,542);
 await pic(s,'D:/数据汇总/数据汇总/v7/frames/frame_0078.jpg',88,246,185,327,'v7在78秒附近的来源陈述画面');
 para(s,'“关不掉”\n“减不掉”',303,266,281,108,33);
 para(s,'定位来源诉求，\n保留调查价值。\n\n制动状态和故障\n原因均未确认。',303,399,281,171,28);
 await pic(s,'D:/数据汇总/数据汇总/v9/frames/frame_0007.jpg',650,247,258,170,'v9上传视频7秒，夜间施工接近');
 await pic(s,'D:/数据汇总/数据汇总/v9/frames/frame_0024.jpg',934,247,258,170,'v9上传视频24秒，白天行驶画面，叠加字幕提及夜间');
 tx(s,'7 秒：夜间施工接近',650,427,258,50,24,C.gray);tx(s,'24 秒：白天对比素材',934,427,258,50,24,C.gray);
 para(s,'E011 保留昼夜不符与素材拼接说明，\n为避免跨素材混用提供复核入口。',650,501,542,89,28);
 foot(s,'核心事实一致可以指来源陈述被正确保留，不能据此确认现实车辆故障。');nn(s,15,'来源报告§4.3–4.4。图片：v7 frames/frame_0078.jpg；v9 frames/frame_0007.jpg与frame_0024.jpg。v9 7秒关联OBS004/E003，24秒关联OBS012/E011。24秒画面白天，字幕称“夜晚驾驶无激光雷达”。');
}
// 16. Traceability metrics beside an actual unverified counterfactual.
{
 const s=slides[15];heading(s,16,'证据追溯与归因辅助');
 label(s,'资料回溯基础',88,193,347);
 tx(s,'254 / 254',88,253,336,59,47,C.blue,true);tx(s,'事件有支持证据与观察',88,320,357,40,27);
 tx(s,'14,733 / 14,733',88,395,357,57,40,C.blue,true);tx(s,'证据引用可解析',88,460,357,40,27);
 tx(s,'724 条假设均未验证',88,542,360,40,29,C.gray);
 label(s,'v9 H012：障碍物识别不足假设',493,193,699);
 table(s,[['要素','假设中的内容'],['改变条件','正确识别前方施工障碍物'],['保持条件','夜间环境、速度、道路与初始状态'],['预期差异','可能减速、停车或避让'],['替代解释','规划 / 执行问题，或驾驶员干预'],['缺失与验证','车辆日志、驾驶员操作；复现光照与路况']],493,249,699,337,[166,533],25);
 foot(s,'引用可解析表示能够找到材料；反事实提供验证思路，相关语义与因果关系仍待核验。');nn(s,16,'来源报告§3.1、§5.1–5.2。v9 E005关联假设H012，verification_status为unverified。正确感知不必然保证规划与执行正确。全部254个事件保留复核要求，1048项复核队列均未关闭。');
}
// 17. Source images establish two narrow, specific failures.
{
 const s=slides[16];heading(s,17,'两个典型偏差');
 label(s,'v1 数值与单位未同步修正',88,190,532);label(s,'v8 说明截图被当作车辆界面',660,190,532);
 await pic(s,'D:/数据汇总/driving_defect_agent/runs/20260927-113112-432900-live/videos/BV14spBzFE2V/derived_media/refine_E026_RF/E026_RF_S029_00_t1.00.jpg',88,241,532,230,'v1补证帧，导航区域为距离路口215米');
 await pic(s,'D:/数据汇总/数据汇总/v8/frames/frame_0007.jpg',660,241,201,333,'v8原始7秒帧，是说明截图，不是车辆原生界面');
 para(s,'补证识别：距离路口 215 米\n最终摘要：215 km/h（或距离读数）',88,495,532,89,28);
 para(s,'补证已识别\n“叠加字幕与截图”\n\n最终摘要仍提升为\n车辆原生界面观察',891,268,301,248,28);
 foot(s,'两例均要求把补证发现回写最终摘要，并同时保留单位、来源层级和限制条件。');nn(s,17,'来源报告§6.1–6.2。v1 E026与E026_REF001；补证图为derived_media/refine_E026_RF/E026_RF_S029_00_t1.00.jpg。v8 E004与E004_REF002，原帧F0007。明确冲突仅针对说明截图被错误提升为原生界面事实，不将同区间的真实车损图一并判错。');
}
// 18. Concrete next work, not a closing/thank-you page.
{
 const s=slides[17];heading(s,18,'后续改进重点');
 table(s,[['优先方向','具体完善内容'],['补证回写与摘要一致性','先解决矛盾，再生成完整核心命题'],['事件聚合与阶段完整性','汇入已观察内容，补齐风险接近等阶段'],['来源与素材范围约束','同步维护截图、口播、原事故与对比素材归属'],['确定性表达统一','区分图标可见、来源声称与机制已验证'],['场景和车辆身份汇总','将可定位陈述写入带来源与不确定性的字段'],['评价基准深化','独立复核、完整标注、重复运行与对照实验']],88,204,1104,374,[359,745],27);
 foot(s,'当前定位：可追溯的线索筛查与研究辅助原型，继续完善最终汇总和验证基础。');nn(s,18,'来源报告§6.3及§7。172条场景陈述、85条车辆身份陈述已存在，但事件级scene和vehicle_identity尚未汇总落地。本页为研究改进方向，不表示本轮已修改Agent或执行对照实验。');
}

const draft=BUILD+'/candidate.pptx';
await (await PresentationFile.exportPptx(p)).save(draft);
await fs.writeFile(BUILD+'/authored_content.json',JSON.stringify(authored,null,2));
await fs.writeFile(BUILD+'/deck.proto.json',JSON.stringify(p.toProto()));
console.log('Exported '+draft+'; slides='+p.slides.count);
for(let i=0;i<slides.length;i++){
 const png=await slides[i].export({format:'png',scale:1.25});
 await fs.writeFile(BUILD+'/render/slide-'+String(i+1).padStart(2,'0')+'.png',new Uint8Array(await png.arrayBuffer()));
 const layout=await slides[i].export({format:'layout'});
 await fs.writeFile(BUILD+'/render/slide-'+String(i+1).padStart(2,'0')+'.json',await layout.text());
 console.log('Rendered '+(i+1));
}
await fs.writeFile(BUILD+'/inspect.ndjson',(await p.inspect({kind:'slide,textbox,shape,image,chart,table,notes',maxChars:150000})).ndjson);
