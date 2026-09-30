import fs from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {finalizePresentation} from 'file:///C:/Users/a1310/.codex/plugins/cache/openai-primary-runtime/presentations/26.909.12148/skills/presentations/container_tools/artifact_tool_utils.mjs';
const req=createRequire('C:/Users/a1310/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/package.json');
req('skia-canvas').FontLibrary.use({'等线 Light':'C:/Windows/Fonts/Dengl.ttf'});
const root='D:/数据汇总/driving_defect_agent/reports/ppt';
const skill='C:/Users/a1310/.codex/plugins/cache/openai-primary-runtime/presentations/26.909.12148/skills/presentations';
const template='C:/Users/a1310/Downloads/城市交通研究院PPT模板.pptx';
process.env.RUNTIME_NODE_MODULES='C:/Users/a1310/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules';
const result=await finalizePresentation({
 workspaceDir:'D:/数据汇总/driving_defect_agent/reports',
 candidatePath:'D:/数据汇总/driving_defect_agent/reports/.ppt-build/candidate-clean.pptx',
 finalPath:root+'/智能驾驶缺陷线索Agent研究汇报_正文_20260927.pptx',
 pythonExecutable:'C:/Users/a1310/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe',
 integrityValidatorPath:skill+'/container_tools/inspect_presentation_package_integrity.py',
 layoutValidatorPath:skill+'/container_tools/inspect_presentation_layout_geometry.py',
 layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit',...[1,4,7,9,10,11,13,16,18].flatMap(n=>['--require-native-table-slide',String(n)])],
 explicitTotalSlideCount:18,
 requiredNativeTableOwnerSlides:[1,4,7,9,10,11,13,16,18],
 requiredNativeChartOwnerSlides:[12],
 materializeLiteralChartWorkbooks:true,
 fontPolicy:{basis:'reference',families:['等线','等线 Light'],referencePath:template,referenceSha256:createHash('sha256').update(await fs.readFile(template)).digest('hex')},
 verifyArtifactToolImport:true,
 receiptPath:'D:/数据汇总/driving_defect_agent/reports/.ppt-build/validation-v4.json'
});
console.log(JSON.stringify(result));
