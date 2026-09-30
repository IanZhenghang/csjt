import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import {FileBlob,PresentationFile} from '@oai/artifact-tool';
const req=createRequire('C:/Users/a1310/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/package.json');
req('skia-canvas').FontLibrary.use({'等线 Light':'C:/Windows/Fonts/Dengl.ttf'});
const build='D:/数据汇总/driving_defect_agent/reports/.ppt-build';
const p=await PresentationFile.importPptx(await FileBlob.load('D:/数据汇总/driving_defect_agent/reports/ppt/智能驾驶缺陷线索Agent研究汇报_正文_20260927.pptx'));
await fs.mkdir(build+'/final-render',{recursive:true});
for(let i=0;i<p.slides.count;i++){
 const png=await p.slides.getItem(i).export({format:'png',scale:1.25});
 await fs.writeFile(build+'/final-render/slide-'+String(i+1).padStart(2,'0')+'.png',new Uint8Array(await png.arrayBuffer()));
}
console.log('Final slides rendered: '+p.slides.count);
const t=await PresentationFile.importPptx(await FileBlob.load('C:/Users/a1310/Downloads/城市交通研究院PPT模板.pptx'));
await fs.mkdir(build+'/template-preview',{recursive:true});
for(let i=0;i<t.slides.count;i++){
 const png=await t.slides.getItem(i).export({format:'png',scale:1});
 await fs.writeFile(build+'/template-preview/slide-'+(i+1)+'.png',new Uint8Array(await png.arrayBuffer()));
}
console.log('Template preview complete');
