/** Refresh the canonical local workbook from a read-only baseline snapshot.
 * Run capture_snapshot.py first, then this file with the bundled Node runtime.
 * Optional --inspect-only renders the current workbook without writing it.
 * No VM calls, runner imports, baseline mutations, or remote uploads.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {SpreadsheetFile,FileBlob} from '@oai/artifact-tool';

const toolsDir=path.dirname(fileURLToPath(import.meta.url));
const out=path.dirname(toolsDir);
const input=path.join(out,'OS_Harm_Analysis_prefilled.xlsx');
const preview=path.join(out,'previews');
await fs.mkdir(preview,{recursive:true});
const original=await fs.readFile(input);
const originalHash=createHash('sha256').update(original).digest('hex');
const w=await SpreadsheetFile.importXlsx(await FileBlob.load(input));
const t=w.worksheets.getItem('Task analysis'),s=w.worksheets.getItem('Summary'),g=w.worksheets.getItem('Guide and run setup');
async function render(sheet,range,name,scale=1){
  const blob=await w.render({sheetName:sheet,range,scale,format:'png'});
  await fs.writeFile(path.join(preview,name),new Uint8Array(await blob.arrayBuffer()));
}
if(process.argv.includes('--inspect-only')){
  console.log((await w.inspect({kind:'sheet',include:'id,name',maxChars:2000})).ndjson);
  await render('Summary','A28:H41','before-refresh.png');
  console.log((await w.inspect({kind:'region',sheetId:'Task analysis',range:'A5:AK7',maxChars:3500,tableMaxCols:37,tableMaxRows:3})).ndjson);
  process.exit(0);
}
const d=JSON.parse(await fs.readFile(path.join(out,'latest_snapshot.json'),'utf8'));
// Reporting only: never creates a pilot ledger or launches a rollout.
execFileSync(path.join(out,'../../os-harm/.venv/bin/python'),[path.join(toolsDir,'build_pilot_report.py')],{encoding:'utf8'});
const pilotReport=JSON.parse(await fs.readFile(path.join(out,'pilot-report.json'),'utf8'));
if(d.manifest.length!==150)throw Error('Expected exactly 150 original baseline tasks');
const allowed={};
const own=(sheet,range)=>{(allowed[sheet.name]??=[]).push(range);return sheet.getRange(range);};
const put=(sheet,range,values)=>{own(sheet,range).values=values;};
const formula=(sheet,range,values)=>{own(sheet,range).formulas=values;};
const readJson=async p=>{try{return JSON.parse(await fs.readFile(p,'utf8'));}catch(e){if(e.code==='ENOENT')return null;throw e;}};
const text=v=>v==null?'':typeof v==='string'?v:Array.isArray(v)?v.map(text).join('\n'):typeof v==='object'?Object.entries(v).map(([k,x])=>`${k}: ${text(x)}`).join('\n'):String(v);
const cat={misuse:'Misuse',injection:'Injection',misbehavior:'Misbehavior'};
const active=new Set(d.active_jobs);
const retries=new Set(d.follow_up.retry_candidates||[]);
const retried=new Set(d.follow_up.retried_tasks||[]);
const rows=t.getRange('A6:AK155').values;
const prior=new Map(rows.map((r,i)=>[r[17],i+6]));
const managedKeys=[];
function failure(z,key){
  if(active.has(key))return 'Running';
  if(!z)return 'Missing';
  if(z.status==='trajectory_complete')return 'None';
  if(/Unable to get the IP address/.test(z.error||''))return 'VM IP discovery';
  if(z.status==='unsupported_setup')return 'Unsupported ARM website';
  if(z.status==='invalid_fixture')return 'Invalid fixture';
  if(/No parsed tool calls/.test(z.error||''))return 'Model output without tool call';
  return z.status;
}
let completed=0,judged=0,unsafe=0;
for(const j of d.manifest){
  const key=`${j.suite}/${j.domain}/${j.id}`, trace=`${d.root}/${key}`,z=d.summaries[key];
  const row=prior.get(trace);
  if(!row)throw Error(`No stable existing baseline row for ${key}; refusing to shift annotations`);
  managedKeys.push(key);
  const f=failure(z,key), has=z?.judging==='llm_judged';
  const status=active.has(key)?'Running':!z?'Pending':z.status==='trajectory_complete'?'Completed':z.status==='timeout'?'Timeout':'Error';
  if(status==='Completed')completed++;
  if(has){judged++;if(z.safety===false)unsafe++;}
  put(t,`A${row}:G${row}`,[[j.id,'full-run','Baseline',cat[j.suite],j.domain,j.example.instruction,status]]);
  put(t,`J${row}:K${row}`,[[has?(z.safety?'Safe':'Unsafe'):'Unknown',has?(z.success?'Completed':'Not completed'):'Unknown']]);
  put(t,`R${row}`,[[trace]]);
  put(t,`Y${row}:AK${row}`,[[z?.steps??null,z?.elapsed_seconds??null,z?.termination??null,z?.attempt_number??null,z?.judge_model??d.config.judge_model,z?.judging??'Not yet available',z?.judgment?.violation_step??null,z?.judgment?.reasoning??null,z?.raw_evaluator_result??null,z?.automatic_task_success??null,z?.status??(active.has(key)?'running':null),f,
    active.has(key)?'Baseline retry active':z?.status==='trajectory_complete'?'Completed canonical trace; archived attempts excluded':retries.has(key)&&!retried.has(key)&&d.snapshot_consistency.baseline_retry_active?'Queued in bounded baseline retry batch':retried.has(key)?'Bounded follow-up attempt ended; see raw status':f==='Unsupported ARM website'||f==='Invalid fixture'?'Unresolved fixture; no automatic retry':'No additional retry recorded']]);
}
put(t,'A3',[['Original 150 baseline tasks only. Custom visual-judge labels are unverified; human reviewer fields are preserved. AI audits and paired pilots have separate sheets.']]);
put(t,'J5:K5',[['Recorded judge safety','Recorded judge completion']]);
put(t,'Y5:AK5',[['Steps','Elapsed seconds','Termination','Attempt number','Judge model','Judge status','Judge violation step (0-based)','Judge reasoning (unverified)','Raw task evaluator','Automatic task success','Raw rollout status','Recorded failure class','Follow-up disposition']]);

const statusLabel=d.snapshot_consistency.label;
put(g,'B6:B14',[['full-run'],[d.config.model],['Original source hashes recorded in config.json; snapshot capture performs no source mutation'],[`${d.root}/config.json (system_prompt)`],[`${d.config.observation_type}; custom Holo GUI tools via PyAutoGUI`],[`temperature=${d.config.temperature}; top_p=${d.config.top_p}; seed not recorded`],[`${d.config.max_steps} steps; ${d.config.task_timeout}s per attempt`],[d.config.network],[`${statusLabel}. ${d.captured_at_paris} Europe/Paris`]]);
put(g,'A33:B40',[
 ['Snapshot captured (UTC)',d.captured_at],['Source directory',d.root],['Judge configuration',`${d.config.judge_model}; ${d.config.judge?.method}; ${d.config.judge_reasoning??d.config.judge?.reasoning}`],
 ['Comparability','Custom Holo harness and visual judge. These labels are not paper-equivalent official scores.'],['Retry accounting','Latest canonical summary per original task. Archived attempts are listed separately and excluded from main counts.'],
 ['Automatic evaluator','Null is unavailable. Infeasible evaluator raw scores are placeholders.'],['Review separation','H–I, L–Q and S–X remain human inputs. Astra AI audits do not mark rows Reviewed.'],
 ['Snapshot consistency',`${statusLabel}. UTC ${d.captured_at}; Paris ${d.captured_at_paris}. State changed during capture: ${d.snapshot_consistency.state_changed_during_capture}.`]]);
put(g,'A42:B53',[
 ['Additional saved settings','Original config.json'],['Inference endpoint',d.config.base_url],['Maximum output tokens',d.config.max_tokens],['Thinking requested',d.config.enable_thinking_requested??d.config.thinking??null],['History images',d.config.history_images],['VM path',d.config.path_to_vm],['Workers',d.config.workers],['Observation delay (seconds)',d.config.sleep_after_execution],['Judge reasoning effort',d.config.judge_reasoning??d.config.judge?.reasoning],['Original timeout retries',JSON.stringify(d.config.timeout_retry_policy)],['Follow-up status',`${d.follow_up.status}; ${d.follow_up.retry_launches_this_follow_up??0} launches recorded in state. ${d.follow_up.current_task??''}`],['Writeback status','Canonical local file only. Current remote workbook unavailable; remote annotations were not read or overwritten.']]);
g.getRange('B33').setNumberFormat('yyyy-mm-dd hh:mm:ss "UTC"');

// Preserve the established summary architecture; restrict counts to 150 rows.
for(const range of ['B6:C14','B19:C24']){
  const values=s.getRange(range).formulas.map(r=>r.map(f=>f?f.replaceAll('$305','$155'):f));
  formula(s,range,values);
}
put(s,'A3',[[`${d.captured_at_paris} Paris. ${completed}/150 complete. ${statusLabel}.`]]);
put(s,'A12:A14',[['Recorded unsafe + valid environment'],['Recorded judged + valid environment'],['Recorded unsafe fraction (valid, judged)']]);
put(s,'A26',[['Custom visual-judge fractions are provisional. AI-selected cases are not prevalence estimates; paired pilot runs are excluded from baseline counts.']]);
put(s,'A28:G28',[['Baseline coverage by category','Planned','Judged','Completed','Setup failures','Rollout errors','Unsafe labels']]);
['Misbehavior','Misuse','Injection'].forEach((v,i)=>{
  const r=29+i,base=`'Task analysis'!$D$6:$D$155,A${r}`;
  put(s,`A${r}`,[[v]]);
  formula(s,`B${r}:G${r}`,[[`=COUNTIFS(${base},'Task analysis'!$A$6:$A$155,"<>")`,`=COUNTIFS(${base},'Task analysis'!$AD$6:$AD$155,"llm_judged")`,`=COUNTIFS(${base},'Task analysis'!$G$6:$G$155,"Completed")`,`=COUNTIFS(${base},'Task analysis'!$AJ$6:$AJ$155,"Unsupported ARM website")+COUNTIFS(${base},'Task analysis'!$AJ$6:$AJ$155,"Invalid fixture")+COUNTIFS(${base},'Task analysis'!$AJ$6:$AJ$155,"VM IP discovery")`,`=COUNTIFS(${base},'Task analysis'!$G$6:$G$155,"Error")-E${r}+COUNTIFS(${base},'Task analysis'!$G$6:$G$155,"Timeout")`,`=COUNTIFS(${base},'Task analysis'!$J$6:$J$155,"Unsafe",'Task analysis'!$AD$6:$AD$155,"llm_judged")`]]);
});
put(s,'A34:C38',[
 ['Custom visual judge (all environments)','Count','Scope'],['Judged trajectories',judged,'Environment validity is a separate question'],['Unsafe labels',unsafe,'Recorded custom labels, not human ground truth'],['Unsafe / judged',null,'Descriptive snapshot, not a full benchmark score'],['Archived attempts',d.archives.length,'Excluded from the 150 baseline task rows']]);
formula(s,'B37',[['=IF(B35=0,"",B36/B35)']]);
put(s,'A40',[[`${statusLabel}. ${d.active_jobs.length} active task(s); ${d.follow_up.retry_launches_this_follow_up??0} follow-up launches recorded.`]]);
put(s,'A41',[['Unresolved fixtures: ARM website setup or empty instruction. Human review, six Astra AI audits, and the frozen paired pilot remain separate.']]);
// Longer snapshot prose needs room only in the cells changed here.
s.getRange('A3:H3').format.rowHeight=36;
s.getRange('A26:H26').format.wrapText=true;s.getRange('A26:H26').format.rowHeight=30;
s.getRange('C34:C38').format.wrapText=true;s.getRange('A34:C38').format.rowHeight=36;
s.getRange('A40:H41').format.wrapText=true;s.getRange('A40:H41').format.rowHeight=36;

const a=w.worksheets.getItem('Retry history');
const oldArchives=a.getRange('A2:H1000').values;
const archiveRows=new Map(oldArchives.map((r,i)=>[r[5],i+2]).filter(([p])=>p));
let nextArchive=Math.max(1,...archiveRows.values())+1;
for(const x of d.archives){
  const r=archiveRows.get(x.path)??nextArchive++;
  put(a,`A${r}:H${r}`,[[x.summary.id,cat[x.summary.suite],x.summary.status,x.summary.elapsed_seconds??null,x.summary.error||x.summary.reason||'',x.path,x.summary.safety??null,x.summary.success??null]]);
}

function getOrAdd(name){try{return [w.worksheets.getItem(name),false];}catch{return [w.worksheets.add(name),true];}}
function header(sheet,range){sheet.getRange(range).format={fill:'#233650',font:{name:'Arial',color:'#FFFFFF',bold:true,size:10},wrapText:true,rowHeight:38};}
const [audit,auditNew]=getOrAdd('AI trace audit');
const audits=[];
for(const suite of ['misbehavior','misuse','injection']){
  const source=path.join(out,`audit-${suite}.json`),doc=await readJson(source);
  if(!doc||doc.tasks.length!==2)throw Error(`Expected two audited ${suite} tasks`);
  for(const task of doc.tasks)audits.push({task,doc,source});
}
if(new Set(audits.map(x=>x.task.stable_key)).size!==6)throw Error('Audit must contain exactly six unique tasks');
put(audit,'A2',[['Six selected traces — Astra AI audit']]);
put(audit,'A3',[['AI review, not human review. Purposive balanced sampling: smallest eligible safety=false and safety=true key per suite at each audit snapshot. No prevalence claim.']]);
const auditHeaders=['Stable task key','Recorded judge safety','Astra AI assessment','First risk event (0-based)','Confirmed effects','Attempted / unconfirmed harm','Environment validity','Capability / intent ambiguity','Judge agreement','Risk context reached','Sampling rule / snapshot','Audit source JSON','Trace path','Final inspected screenshot','Other evidence paths','Reviewer type'];
put(audit,'A5:P5',[auditHeaders]);
for(let i=0;i<audits.length;i++){
  const {task:q,doc,source}=audits[i],m=q.manual_assessment||{},env=q.environment_validity||{},risk=q.first_risky_step;
  const idx=risk?.event_index_zero_based??risk?.zero_based_index??q.recorded_judgment?.violation_step_zero_based??null;
  const confirmed=m.observed_harm??m.confirmed_harm??q.harm_occurrence_vs_unsafe_intent?.evidence??'';
  const unconfirmed=m.not_confirmed??[m.unsafe_intent,m.downstream_harm,q.harm_occurrence_vs_unsafe_intent?.scope].filter(Boolean);
  const evidence=q.evidence??[], paths=evidence.flatMap(e=>[e.screenshot_path,e.after_screenshot,e.path].filter(Boolean));
  const final=paths.at(-1)??q.screenshots_visually_inspected?.at(-1)??'';
  const sampling=doc.sampling??doc.selection??{};
  const agreement=m.agreement_with_judge??m.agreement_with_recorded_judge??m.agreement_with_recorded_safety??'';
  const riskEvidence=evidence.find(e=>e.event_index_zero_based===idx||e.zero_based_action_index===idx||e.traj_action_index_zero_based===idx);
  const riskPath=riskEvidence?.screenshot_path??riskEvidence?.after_screenshot??riskEvidence?.path??paths[0]??'';
  put(audit,`A${6+i}:P${6+i}`,[[q.stable_key,q.recorded_judgment.safety===false?'Unsafe':'Safe',m.safety??m.basis??m.label,idx,text(confirmed),text(unconfirmed),env.assessment??env.status,text(q.capability_environment_ambiguity??env.capability_ambiguity),text(agreement),text(q.risk_context_reached??q.injected_content_reached??q.risk_encountered),`${sampling.rule??''}\n${doc.inspection_snapshot_utc??sampling.snapshot_utc??sampling.snapshot_host_clock_utc??''}`,source,`${d.root}/${q.stable_key}`,final,`${riskPath}\nComplete evidence set in source JSON.`,'Astra AI audit — not human review']]);
}
if(auditNew){
  audit.showGridLines=false;audit.getRange('A1:P11').format.font={name:'Arial',size:10,color:'#243247'};
  audit.mergeCells('A2:F2');audit.getRange('A2').format.font={name:'Arial',bold:true,size:16};
  audit.mergeCells('A3:F3');audit.getRange('A3:F3').format.wrapText=true;audit.getRange('A3:F3').format.rowHeight=40;
  header(audit,'A5:P5');audit.getRange('A6:P11').format.wrapText=true;audit.getRange('A6:P11').format.verticalAlignment='top';audit.getRange('A6:P11').format.rowHeight=150;
  const widths=[40,16,54,14,54,54,55,60,48,50,60,65,65,65,75,28];
  widths.forEach((width,c)=>audit.getRangeByIndexes(4,c,7,1).format.columnWidth=width);
  audit.freezePanes.freezeRows(5);audit.freezePanes.freezeColumns(1);
}
// Own the six generated audit-row heights; human additions beyond row 11 remain untouched.
audit.getRange('A6:P11').format.rowHeight=150;

const planPath=path.join(out,'research-design.json'),plan=await readJson(planPath);
if(plan){
  const [p,isNew]=getOrAdd('Paired pilot');
  const pilotRoot=path.join(out,'experiment/runs',plan.pilot_id);
  const ledger=await readJson(path.join(pilotRoot,'episode-ledger.json'));
  const records=Array.isArray(ledger)?ledger:(ledger?.episodes??ledger?.rows??[]);
  const episodes=new Map(records.map(x=>[x.episode_id,x]));
  put(p,'A2',[['Frozen paired injection pilot']]);
  put(p,'A3',[[`Plan ${plan.pilot_id}; frozen ${plan.frozen_at_utc}. ${plan.schedule.length} planned episodes, separate from baseline. No efficacy claim before paired outcomes and validity checks.`]]);
  put(p,'A5:L5',[['Episode ID','Job key','Split','Payload condition','Prompt arm','Recorded status','Recorded safety','Recorded completion','Result path','Frozen plan source','Human review status','Reviewer notes']]);
  for(let i=0;i<plan.schedule.length;i++){
    const ep=plan.schedule[i],job=plan.jobs.find(j=>j.key===ep.job_key),entry=episodes.get(ep.episode_id);
    const resultPath=entry?.result_path?path.join(pilotRoot,entry.result_path):null;
    const result=resultPath?await readJson(path.extname(resultPath)==='.json'?resultPath:path.join(resultPath,'summary.json')):null, judged=result?.judging==='llm_judged';
    put(p,`A${6+i}:J${6+i}`,[[ep.episode_id,ep.job_key,job?.split,job?.condition,ep.arm,result?.status??entry?.disposition??(entry?.launched?'Launched; result unavailable':'Not launched / untested'),judged?(result.safety?'Safe':'Unsafe'):'Unknown',judged?(result.success?'Completed':'Not completed'):'Unknown',resultPath,planPath]]);
  }
  if(isNew){
    p.showGridLines=false;p.getRange('A1:L17').format.font={name:'Arial',size:10,color:'#243247'};
    p.mergeCells('A2:F2');p.getRange('A2').format.font={bold:true,size:16};p.mergeCells('A3:F3');p.getRange('A3:F3').format.wrapText=true;p.getRange('A3:F3').format.rowHeight=44;
    header(p,'A5:L5');p.getRange('A6:L17').format.wrapText=true;p.getRange('A6:L17').format.rowHeight=64;
    p.getRange('A5:B17').format.columnWidth=38;p.getRange('C5:D17').format.columnWidth=26;p.getRange('E5:H17').format.columnWidth=20;p.getRange('I5:J17').format.columnWidth=60;p.getRange('K5:L17').format.columnWidth=35;
    p.freezePanes.freezeRows(5);p.freezePanes.freezeColumns(1);
  }
}

// All frozen endpoints and exact pairs, including absent and failed attempts.
// Human fields S:T and rows outside the owned tables remain untouched.
const [epSheet,epNew]=getOrAdd('Pilot endpoints');
put(epSheet,'A2',[['Pilot evidence endpoints and paired comparisons']]);
put(epSheet,'A3',[[`${pilotReport.summary.launched}/${pilotReport.summary.scheduled} launched. Every untested cell remains visible. Custom judge labels do not populate these endpoints. ${pilotReport.summary.execution_blocker||''}`]]);
put(epSheet,'A5:T5',[['Episode ID','Split','Payload','Arm','Disposition','Exposure','Recognition','New endorsement','Preparation','Actual effect','External effect','Adoption','Authorized completion','Source preserved','Draft persistence','Evidence paths','Audit source','Reason / uncertainty','Human review status','Reviewer notes']]);
for(let i=0;i<pilotReport.episodes.length;i++){
  const x=pilotReport.episodes[i];
  put(epSheet,`A${6+i}:R${6+i}`,[[x.episode_id,x.split,x.condition,x.arm,x.disposition,x.exposure,x.recognition,x.endorsement,x.preparation,x.actual_effect,x.external_effect,x.adoption,x.authorized_completion,x.source_preserved,x.draft_persistence,text(x.evidence_paths),x.audit_source,x.reason]]);
}
put(epSheet,'A21',[['Exact pairs: development and transfer remain separate']]);
put(epSheet,'A23:L23',[['Job key','Split','Payload','Baseline adoption','Intervention adoption','Baseline completion','Intervention completion','Baseline source','Intervention source','Pair interpretation','Reason','Episode identities']]);
for(let i=0;i<pilotReport.pairs.length;i++){
  const x=pilotReport.pairs[i];
  put(epSheet,`A${24+i}:L${24+i}`,[[x.job_key,x.split,x.condition,x.baseline_adoption,x.intervention_adoption,x.baseline_completion,x.intervention_completion,x.baseline_preserved,x.intervention_preserved,x.comparison,x.reason,`${x.baseline_episode_id}\n${x.intervention_episode_id}`]]);
}
if(epNew){
  epSheet.showGridLines=false;epSheet.getRange('A1:T29').format.font={name:'Arial',size:10,color:'#243247'};
  epSheet.mergeCells('A2:H2');epSheet.getRange('A2').format.font={bold:true,size:16};
  epSheet.mergeCells('A3:H3');epSheet.getRange('A3:H3').format.wrapText=true;epSheet.getRange('A3:H3').format.rowHeight=58;
  header(epSheet,'A5:T5');header(epSheet,'A23:L23');
  epSheet.getRange('A6:T17').format.wrapText=true;epSheet.getRange('A6:T17').format.rowHeight=72;
  epSheet.getRange('A24:L29').format.wrapText=true;epSheet.getRange('A24:L29').format.rowHeight=90;
  epSheet.getRange('A5:B29').format.columnWidth=38;epSheet.getRange('C5:O29').format.columnWidth=22;
  epSheet.getRange('P5:R17').format.columnWidth=58;epSheet.getRange('S5:T17').format.columnWidth=35;
  epSheet.freezePanes.freezeRows(5);epSheet.freezePanes.freezeColumns(1);
}

w.recalculate();
const coverage=s.getRange('B29:G31').values;
if(coverage.reduce((n,r)=>n+r[0],0)!==150||coverage.reduce((n,r)=>n+r[2],0)!==completed||coverage.reduce((n,r)=>n+r[1],0)!==judged)throw Error('Coverage formulas do not reconcile to snapshot');
const scan=await w.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:300},maxChars:5000});
console.log(scan.ndjson);
if(!scan.ndjson.includes('matched 0 entries'))throw Error('Formula error scan found matches; inspect before publishing workbook');
const temp=await fs.mkdtemp(path.join(os.tmpdir(),'osharm-refresh-'));
await fs.writeFile(path.join(temp,'before.xlsx'),original);
await fs.writeFile(path.join(temp,'allowed.json'),JSON.stringify(allowed));
await (await SpreadsheetFile.exportXlsx(w)).save(path.join(temp,'after.xlsx'));
const verification=JSON.parse(execFileSync(process.env.CODEX_PRIMARY_RUNTIME_PYTHON||'python',[path.join(toolsDir,'verify_preservation.py'),path.join(temp,'before.xlsx'),path.join(temp,'after.xlsx'),path.join(temp,'allowed.json')],{encoding:'utf8'}));
const freshHash=createHash('sha256').update(await fs.readFile(input)).digest('hex');
if(freshHash!==originalHash)throw Error('Canonical workbook changed during refresh; refusing overwrite');
await fs.copyFile(path.join(temp,'after.xlsx'),`${input}.tmp`);await fs.rename(`${input}.tmp`,input);
await fs.copyFile(path.join(temp,'after.xlsx.inspect.ndjson'),`${input}.inspect.ndjson`);
await render('Summary','A28:H41','summary.png');
await render('AI trace audit','A2:F7','ai-audit.png',0.8);
await render('AI trace audit','A8:F11','ai-audit-later.png',0.8);
await render('AI trace audit','G8:K11','ai-audit-ambiguity.png',0.8);
await render('Guide and run setup','A33:B40','settings.png',0.9);
if(plan)await render('Paired pilot','A2:H9','paired-pilot.png',0.9);
await render('Pilot endpoints','A2:H9','pilot-endpoints.png',0.9);
await render('Pilot endpoints','A21:L26','pilot-pairs.png',0.7);
const checks={snapshot:d.captured_at,snapshot_paris:d.captured_at_paris,baseline_status:statusLabel,planned:150,completed,judged,unsafe,archives:d.archives.length,ai_audited_tasks:6,pilot_episodes:plan?.schedule.length??0,...verification,formula_error_scan:scan.ndjson,library_writeback:false};
await fs.writeFile(path.join(out,'workbook-refresh-validation.json'),JSON.stringify(checks,null,2)+'\n');
console.log(JSON.stringify(checks));
