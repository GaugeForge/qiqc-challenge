document.querySelector('#copy-code').addEventListener('click',async()=>{
 const status=document.querySelector('#copy-status');
 try{await navigator.clipboard.writeText(document.querySelector('#install-code').textContent);status.textContent='Commands copied.';}
 catch{status.textContent='Clipboard unavailable. Select the commands to copy them.';}
});


// Exact ideal two-level probabilities; drive varies BETWEEN independent pulses.
// u=t/Tref, d=Δ/Ωref, a=Ω/Ωref. Opacity is decorative, not a probability scale.
{
 const hero=document.querySelector('.hero');
 const field=document.querySelector('.hero-field');
 const canvas=document.querySelector('#rabi-canvas');
 const ctx=canvas.getContext('2d');
 const source=document.createElement('canvas'),sourceCtx=source.getContext('2d',{alpha:false});
 const reveal=document.createElement('canvas'),revealCtx=reveal.getContext('2d');
 const play=document.querySelector('#rabi-play');
 const caption=document.querySelector('#rabi-caption');
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 const colors=[[245,247,248],[153,217,208],[35,142,133],[19,61,59],[250,171,113]];
 const lut=new Uint8ClampedArray(3072);
 for(let i=0;i<1024;i++){
  const v=i/1023*4,k=Math.min(3,Math.floor(v)),f=v-k;
  for(let c=0;c<3;c++)lut[i*3+c]=colors[k][c]*(1-f)+colors[k+1][c]*f;
 }
 let width=1000,height=500,pixels,phase=0,last=null,lastPaint=0,frame=null;
 let playing=!reduced.matches,visible=true,dirty=true,hovered=false;
 let targetX=.8,targetY=.5,x=.8,y=.5,strength=0;
 function drawField(){
  const a=1+.3*Math.sin(phase*Math.PI/10),aa=a*a,data=pixels.data;
  for(let row=0;row<height;row++){
   const d=2.4-4.8*row/(height-1),q=aa+d*d,scale=aa/q,w=Math.PI*Math.sqrt(q)*3.5/(width-1);
   for(let col=0;col<width;col++){
    const s=Math.sin(w*col),color=Math.round(scale*s*s*1023)*3,index=(row*width+col)*4;
    data[index]=lut[color];data[index+1]=lut[color+1];data[index+2]=lut[color+2];data[index+3]=255;
   }
  }
  sourceCtx.putImageData(pixels,0,0);dirty=false;
 }
 function composite(){
  ctx.clearRect(0,0,width,height);ctx.globalAlpha=.42;ctx.drawImage(source,0,0);ctx.globalAlpha=1;
  if(strength>.001){
   revealCtx.clearRect(0,0,width,height);revealCtx.globalCompositeOperation='source-over';revealCtx.drawImage(source,0,0);
   const r=width*.20*(1+.035*Math.sin(phase*1.7));
   const gradient=revealCtx.createRadialGradient(x*width,y*height,r*.14,x*width,y*height,r);
   gradient.addColorStop(0,'rgba(0,0,0,'+strength+')');gradient.addColorStop(.24,'rgba(0,0,0,'+strength*.97+')');gradient.addColorStop(.6,'rgba(0,0,0,'+strength*.4+')');gradient.addColorStop(1,'transparent');
   revealCtx.globalCompositeOperation='destination-in';revealCtx.fillStyle=gradient;revealCtx.fillRect(0,0,width,height);
   revealCtx.globalCompositeOperation='source-over';ctx.drawImage(reveal,0,0);
  }
  field.classList.add('ready');
 }
 function tick(now){
  frame=null;if(document.hidden||!visible){last=null;return;}
  const delta=last===null?0:Math.min((now-last)/1000,.08);last=now;
  if(playing){phase+=delta;dirty=true;}
  const ease=reduced.matches?1:1-Math.exp(-delta*12);
  x+=(targetX-x)*ease;y+=(targetY-y)*ease;strength+=((hovered?1:0)-strength)*ease;
  if(now-lastPaint>=1000/24){if(dirty)drawField();composite();lastPaint=now;}
  if(playing||Math.abs(strength-(hovered?1:0))>.001||Math.abs(x-targetX)+Math.abs(y-targetY)>.001)frame=requestAnimationFrame(tick);
 }
 function schedule(){if(frame!==null||!visible||document.hidden)return;last=null;frame=requestAnimationFrame(tick);}
 function resize(){
  // Use layout size: scroll scaling must not reallocate or resample the simulation.
  const box={width:canvas.offsetWidth,height:canvas.offsetHeight};width=Math.min(1200,Math.max(400,Math.round(box.width)));height=Math.max(200,Math.round(width*box.height/box.width));
  canvas.width=source.width=reveal.width=width;canvas.height=source.height=reveal.height=height;
  pixels=sourceCtx.createImageData(width,height);dirty=true;drawField();composite();schedule();
 }
 hero.addEventListener('pointermove',event=>{
  const box=canvas.getBoundingClientRect();
  const px=(event.clientX-box.left)/box.width,py=(event.clientY-box.top)/box.height;
  const inside=px>=0&&px<=1&&py>=0&&py<=1;
  const captionBox=caption.getBoundingClientRect();
  const nearCaption=event.clientX>=captionBox.left&&event.clientX<=captionBox.right&&event.clientY>=box.bottom&&event.clientY<=captionBox.bottom;
  hero.classList.toggle('field-caption-visible',inside||nearCaption);
  targetX=Math.max(0,Math.min(1,px));targetY=Math.max(0,Math.min(1,py));
  // Suppress the full-opacity lens when the pointer is over protected text/CTAs.
  // The broad page wash and glyph-shaped text shadows preserve readability.
  hovered=inside&&!event.target.closest('.protected');schedule();
 });
 hero.addEventListener('pointerleave',()=>{hovered=false;hero.classList.remove('field-caption-visible');schedule();});
 function syncPlay(){play.textContent=playing?'Ⅱ  Pause motion':'▷  Play motion';play.setAttribute('aria-label',playing?'Pause background animation':'Play background animation');schedule();}
 play.addEventListener('click',()=>{playing=!playing;syncPlay();});
 reduced.addEventListener('change',event=>{if(event.matches){playing=false;syncPlay();}});
 document.addEventListener('visibilitychange',()=>{if(document.hidden&&frame!==null){cancelAnimationFrame(frame);frame=null;}schedule();});
 new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;if(!visible&&frame!==null){cancelAnimationFrame(frame);frame=null;}schedule();}).observe(hero);
 resize();new ResizeObserver(resize).observe(field);syncPlay();
}

// Brief exit/entry transitions keep example changes legible; rapid choices use the latest request.
const contentMotion=matchMedia('(prefers-reduced-motion: reduce)');
const contentTransitions=new Map();
async function transitionContent(key,elements,update){
 const opacity=elements.map(element=>getComputedStyle(element).opacity);
 const previous=contentTransitions.get(key);
 if(previous)previous.animations.forEach(animation=>animation.cancel());
 const job={animations:[],committed:false,update};
 contentTransitions.set(key,job);
 if(contentMotion.matches||elements.some(element=>!element.animate)){
  update();contentTransitions.delete(key);return;
 }
 job.animations=elements.map((element,index)=>element.animate([
  {opacity:opacity[index],transform:'translateY(0)'},{opacity:0,transform:'translateY(-4px)'}
 ],{duration:110,easing:'ease-out',fill:'forwards'}));
 await Promise.all(job.animations.map(animation=>animation.finished.catch(()=>{})));
 if(contentTransitions.get(key)!==job)return;
 job.animations.forEach(animation=>animation.cancel());
 const incoming=update()||elements;job.committed=true;
 job.animations=incoming.map((element,index)=>element.animate([
  {opacity:0,transform:'translateY(9px)'},{opacity:1,transform:'translateY(0)'}
 ],{duration:380,delay:index*35,easing:'cubic-bezier(.22,1,.36,1)',fill:'both'}));
 await Promise.all(job.animations.map(animation=>animation.finished.catch(()=>{})));
 if(contentTransitions.get(key)===job){job.animations.forEach(animation=>animation.cancel());contentTransitions.delete(key);}
}
contentMotion.addEventListener('change',event=>{
 if(!event.matches)return;
 for(const job of contentTransitions.values()){
  job.animations.forEach(animation=>animation.cancel());if(!job.committed)job.update();
 }
 contentTransitions.clear();
});

// Task stages are directly selectable; they do not simulate evaluation attempts.
{
 const tabs=[...document.querySelectorAll('[data-task-step]')];
 const panel=document.querySelector('#task-panel');
 const stages=[
  ['Understand the objective.','Read the task instructions and device specifications. Identify the quantity to estimate or the operation to calibrate, the available tools and the limits on experiments.',['Task brief','Tools & limits','Success criteria'],'Start with a clear target and an experiment budget.'],
  ['Plan useful experiments.','Your harness uses HY4 to choose which measurements to request and how to allocate the available budget. Plan for evidence that can distinguish competing explanations or improve a calibration.',['Current knowledge','Experiment plan','Budget allocation'],'Make each experiment serve the task objective.'],
  ['Run, inspect and adapt.','Call the approved laboratory tools, retrieve their results and use the evidence to decide what to try next. Adapt the strategy within the task limits instead of treating the first result as a final answer.',['Tool calls','Measurements','Updated strategy'],'Turn experimental evidence into the next decision.'],
  ['Submit a verifiable answer.','Return the task-specific answer in the required format, including uncertainty where requested. A separate verifier checks the outcome against recorded evidence. This task answer is distinct from submitting your harness to the competition.',['Final answer','Recorded evidence','Verifier checks'],'Success is a result the verifier can check.']
 ];
 let selected=0;
 function select(index,focus=false){
  if(focus)tabs[index].focus();
  if(selected===index)return;selected=index;
  tabs.forEach((tab,i)=>{tab.setAttribute('aria-selected',String(i===index));tab.tabIndex=i===index?0:-1;});
  transitionContent(panel,[panel],()=>{
   const [title,copy,nodes,outcome]=stages[index];
   panel.setAttribute('aria-labelledby',tabs[index].id);
   document.querySelector('#task-stage-title').textContent=title;
   document.querySelector('#task-stage-copy').textContent=copy;
   nodes.forEach((text,i)=>document.getElementById('task-node-'+['a','b','c'][i]).textContent=text);
   document.querySelector('#task-stage-outcome').textContent=outcome;
  });
 }
 tabs.forEach((tab,index)=>{
  tab.addEventListener('click',()=>select(index));
  tab.addEventListener('keydown',event=>{
   const next=event.key==='ArrowRight'?(index+1)%tabs.length:event.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:event.key==='Home'?0:event.key==='End'?tabs.length-1:null;
   if(next===null)return;event.preventDefault();select(next,true);
  });
 });
}

// Reference: workshop-preview/dist/motion.js. Animate inner spans so hit boxes
// and document layout remain stable. Text selection always takes priority.
for(const title of document.querySelectorAll('#hero-title')){
 const items=[...title.querySelectorAll('.title-line')].map(target=>({target,surface:target.querySelector('.motion-surface')}));
 const motion=matchMedia('(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)');
 let frame=null,selecting=false;
 const pending=new Map();
 function resetItem(item){pending.delete(item);item.target.classList.remove('is-hovered');item.surface.style.removeProperty('--shift-x');item.surface.style.removeProperty('--shift-y');}
 function reset(){if(frame!==null)cancelAnimationFrame(frame);frame=null;items.forEach(resetItem);}
 function render(){
  frame=null;if(!motion.matches||selecting)return;
  for(const [item,point] of pending){
   const rect=item.target.getBoundingClientRect();
   const x=Math.max(-1,Math.min(1,(point.x-rect.left)/rect.width*2-1));
   const y=Math.max(-1,Math.min(1,(point.y-rect.top)/rect.height*2-1));
   item.surface.style.setProperty('--shift-x',(x*8)+'px');
   item.surface.style.setProperty('--shift-y',(y*4.8)+'px');
   item.target.classList.add('is-hovered');
  }
  pending.clear();
 }
 items.forEach(item=>{
  item.target.addEventListener('pointermove',event=>{
   if(!motion.matches||selecting||event.buttons||event.pointerType==='touch')return;
   pending.set(item,{x:event.clientX,y:event.clientY});if(frame===null)frame=requestAnimationFrame(render);
  });
  item.target.addEventListener('pointerleave',()=>resetItem(item));
  item.target.addEventListener('pointercancel',()=>resetItem(item));
 });
 title.addEventListener('pointerdown',()=>{selecting=true;title.classList.add('is-selecting');reset();});
 function endSelection(){selecting=false;title.classList.remove('is-selecting');}
 window.addEventListener('pointerup',endSelection);window.addEventListener('pointercancel',endSelection);
 window.addEventListener('scroll',reset,{passive:true});
 window.addEventListener('blur',()=>{endSelection();reset();});
 motion.addEventListener('change',reset);
 document.addEventListener('visibilitychange',()=>{if(document.hidden){endSelection();reset();}});
}

// Registration remains an explicit inquiry until a real form URL is configured.
{
 const trigger=document.querySelector('#register-now');
 const dialog=document.querySelector('#registration-dialog');
 if(trigger&&dialog){
  let closing=false;
  trigger.addEventListener('click',()=>{
   dialog.showModal();
   if(!contentMotion.matches)dialog.animate([{opacity:0,transform:'translateY(14px) scale(.98)'},{opacity:1,transform:'translateY(0) scale(1)'}],{duration:340,easing:'cubic-bezier(.22,1,.36,1)'});
  });
  async function closeDialog(){
   if(closing)return;closing=true;
   if(!contentMotion.matches){
    dialog.getAnimations().forEach(animation=>animation.cancel());
    await dialog.animate([{opacity:1,transform:'translateY(0)'},{opacity:0,transform:'translateY(8px)'}],{duration:150,easing:'ease-in'}).finished.catch(()=>{});
   }
   dialog.close();closing=false;
  }
  dialog.querySelector('.dialog-close').addEventListener('click',closeDialog);
  dialog.addEventListener('cancel',event=>{event.preventDefault();closeDialog();});
  dialog.addEventListener('close',()=>trigger.focus());
 }
}

// Align the section boundary (including its divider) below the sticky header.
// Pixel scrolling avoids stacking scroll-padding with negative scroll margins.
{
 const header=document.querySelector('.site-header');
 const root=document.documentElement;
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 let queued=null;
 function updateHeader(){root.style.setProperty('--nav-height',header.getBoundingClientRect().height+'px');}
 function resolve(hash){
  if(!hash||hash==='#')return null;
  let id;try{id=decodeURIComponent(hash.slice(1));}catch{return null;}
  id=({'rules':'task','how-it-works':'task','evaluation':'task'})[id]||id;
  const target=document.getElementById(id);
  if(!target)return null;
  const heading=target.matches('.hero, .hero-stage')?target.querySelector('h1, h2'):target.matches('.section')?target.querySelector('.section-heading, .eyebrow, h2'):target;
  return {target,heading:heading||target};
 }
 function navigate(hash,{push=false,smooth=false,focus=false}={}){
  const resolved=resolve(hash);if(!resolved)return;
  updateHeader();
  let absoluteTop=resolved.target.getBoundingClientRect().top+window.scrollY;
  const stage=resolved.target.closest('.hero-stage');
  if(stage&&stage!==resolved.target){
   // Internal hero links address the revealed layout, beyond the opening motion.
   absoluteTop=stage.getBoundingClientRect().top+window.scrollY+Number(stage.dataset.scrollDistance||0)
    +resolved.target.getBoundingClientRect().top-stage.querySelector('.hero').getBoundingClientRect().top;
  }
  const inset=header.getBoundingClientRect().bottom;
  const maxTop=Math.max(0,root.scrollHeight-window.innerHeight);
  const top=Math.max(0,Math.min(maxTop,absoluteTop-inset));
  if(push&&location.hash!==hash)history.pushState(null,'',hash);
  if(focus){
   if(!resolved.heading.hasAttribute('tabindex')){
    resolved.heading.setAttribute('tabindex','-1');
    resolved.heading.addEventListener('blur',()=>resolved.heading.removeAttribute('tabindex'),{once:true});
   }
   resolved.heading.focus({preventScroll:true});
  }
  window.scrollTo({top,behavior:smooth&&!reduced.matches?'smooth':'instant'});
 }
 function queue(hash,options){
  if(queued!==null)cancelAnimationFrame(queued);
  queued=requestAnimationFrame(()=>{queued=null;navigate(hash,options);});
 }
 document.addEventListener('click',event=>{
  if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
  const link=event.target.closest('a[href^="#"]');
  if(!link||link.hasAttribute('download')||link.target==='_blank')return;
  const hash=link.getAttribute('href');if(!resolve(hash))return;
  event.preventDefault();setMenu(false);
  // Wait for the collapsed navigation to have its final geometry.
  queue(hash,{push:true,smooth:true,focus:event.detail===0});
 });
 const restore=()=>queue(location.hash,{smooth:false});
 window.addEventListener('popstate',restore);
 window.addEventListener('hashchange',restore);
 updateHeader();new ResizeObserver(updateHeader).observe(header);
 const initialHash=location.hash;
 const navigationType=performance.getEntriesByType('navigation')[0]?.type;
 const isReload=navigationType==='reload';
 const savedPosition=history.state?.qiqcScroll;
 const canRestore=isReload&&savedPosition?.url===location.href&&Number.isFinite(savedPosition.y);
 // The URL hash can be an old nav click, not the section currently being read.
 // Save the actual position on departure without adding a history entry.
 window.addEventListener('pagehide',()=>{
  history.replaceState({...history.state,qiqcScroll:{url:location.href,x:window.scrollX,y:window.scrollY}},'');
 });
 let userMoved=false;
 const cancelInitialRestore=()=>{userMoved=true;};
 const inputEvents=['wheel','touchstart','pointerdown','keydown'];
 inputEvents.forEach(type=>window.addEventListener(type,cancelInitialRestore,{passive:true,once:true}));
 const loaded=document.readyState==='complete'?Promise.resolve():new Promise(resolve=>window.addEventListener('load',resolve,{once:true}));
 Promise.all([loaded,document.fonts?document.fonts.ready:Promise.resolve()]).then(()=>{
  // Let the hero's font-dependent height finish updating before restoring pixels.
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
   inputEvents.forEach(type=>window.removeEventListener(type,cancelInitialRestore));
   if(userMoved||location.hash!==initialHash)return;
   if(canRestore){
    window.scrollTo({left:savedPosition.x||0,top:savedPosition.y,behavior:'instant'});
   }else if(!isReload&&navigationType!=='back_forward'&&initialHash){
    queue(initialHash,{smooth:false});
   }
   // On reloads without a saved position, preserve the browser's native restoration.
  }));
 });
}

// Count only after the overview transition finishes and the amount is actually visible.
{
 const numbers=[...document.querySelectorAll('.prize-number[data-amount]')];
 const stage=document.querySelector('.hero-stage');
 const header=document.querySelector('.site-header');
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 const formatter=new Intl.NumberFormat('en-US');
 const states=new Map(numbers.map(number=>[number,{elapsed:0,last:null,frame:null,done:false}]));
 let checkFrame=null;
 function pause(state){
  if(state.frame!==null)cancelAnimationFrame(state.frame);
  state.frame=null;state.last=null;
 }
 function finish(number,state){
  pause(state);state.done=true;
  number.textContent=formatter.format(Number(number.dataset.amount));
  observer.unobserve(number);
 }
 function isVisible(number){
  if(document.hidden||stage.dataset.overviewReady!=='true'||number.closest('[inert]'))return false;
  const box=number.getBoundingClientRect();
  const top=header.getBoundingClientRect().bottom;
  const visibleHeight=Math.max(0,Math.min(box.bottom,window.innerHeight)-Math.max(box.top,top));
  return box.width>0&&box.height>0&&visibleHeight/box.height>=.75;
 }
 function tick(number,state,now){
  state.frame=null;
  if(!isVisible(number)){state.last=null;return;}
  if(state.last!==null)state.elapsed+=Math.max(0,now-state.last);
  state.last=now;
  const progress=Math.min(1,state.elapsed/1700);
  number.textContent=formatter.format(Math.round(Number(number.dataset.amount)*(1-Math.pow(1-progress,3))));
  if(progress===1)finish(number,state);
  else state.frame=requestAnimationFrame(time=>tick(number,state,time));
 }
 function check(){
  checkFrame=null;
  for(const [number,state] of states){
   if(state.done)continue;
   if(reduced.matches){finish(number,state);continue;}
   if(!isVisible(number)){pause(state);continue;}
   if(state.frame===null)state.frame=requestAnimationFrame(time=>tick(number,state,time));
  }
 }
 function schedule(){if(checkFrame===null)checkFrame=requestAnimationFrame(check);}
 const observer=new IntersectionObserver(schedule,{threshold:[0,.75,1]});
 numbers.forEach(number=>{
  if(reduced.matches)finish(number,states.get(number));
  else{number.textContent='0';observer.observe(number);}
 });
 document.addEventListener('hero:overviewchange',schedule);
 window.addEventListener('scroll',schedule,{passive:true});
 window.addEventListener('resize',schedule,{passive:true});
 window.addEventListener('pageshow',schedule);
 reduced.addEventListener('change',schedule);
 document.addEventListener('visibilitychange',()=>{
  if(document.hidden)states.forEach(pause);
  else schedule();
 });
 schedule();
}

// Move the original text into place through a sticky scroll stage; never clone it.
{
 const stage=document.querySelector('.hero-stage');
 const hero=stage.querySelector('.hero');
 const field=stage.querySelector('.hero-field');
 const header=document.querySelector('.site-header');
 const details=stage.querySelector('.entry-basics');
 const caption=stage.querySelector('.field-caption');
 const title=stage.querySelector('#hero-title');
 const lines=[...title.querySelectorAll('.title-line')];
 const intro=[...stage.querySelectorAll('.intro-line')];
 const slogan=stage.querySelector('.hero-slogan');
 const items=[...lines,...intro,slogan];
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 let geometry=[],distance=0,start=0,frame=null,resizeFrame=null,announced=false;
 let fieldScaleX=1,fieldScaleY=1;
 const clamp=value=>Math.max(0,Math.min(1,value));
 const ease=value=>value*value*(3-2*value);
 function reveal(value){
  stage.style.setProperty('--details-reveal',String(value));
  const available=value>=.85;
  details.inert=caption.inert=!available;
  if(available&&!announced){announced=true;document.dispatchEvent(new Event('hero:revealed'));}
 }
 function setOverviewReady(ready){
  if(stage.dataset.overviewReady===String(ready))return;
  stage.dataset.overviewReady=String(ready);
  document.dispatchEvent(new Event('hero:overviewchange'));
 }
 function render(){
  frame=null;
  if(reduced.matches){reveal(1);setOverviewReady(true);return;}
  const scrollProgress=clamp((window.scrollY-start)/distance);
  const progress=ease(scrollProgress);
  stage.style.setProperty('--field-scale-x',String(1+(fieldScaleX-1)*(1-progress)));
  stage.style.setProperty('--field-scale-y',String(1+(fieldScaleY-1)*(1-progress)));
  stage.style.setProperty('--field-mask-x',(50+25*progress)+'%');
  stage.style.setProperty('--field-mask-y',(42-20*progress)+'%');
  stage.style.setProperty('--wash-x',(50-26*progress)+'%');
  stage.style.setProperty('--wash-y',(42-17*progress)+'%');
  geometry.forEach(({element,x,y,scale},index)=>{
   const remainder=1-progress;
   let tx=x*remainder,ty=y*remainder;
   if(index>0&&index<lines.length){
    // Separate the lines vertically before bringing the second line left.
    const drop=ease(clamp(progress/.32));
    const move=ease(clamp((progress-.32)/.68));
    tx=x*(1-move);
    ty=geometry[0].y*remainder-(geometry[0].y-y)*(1-drop);
   }
   element.style.transform=`translate(${tx}px,${ty}px) scale(${1+(scale-1)*remainder})`;
  });
  reveal(ease(clamp((progress-.28)/.72)));
  setOverviewReady(scrollProgress>=1);
 }
 function measure(){
  resizeFrame=null;
  items.forEach(element=>element.style.removeProperty('transform'));
  if(reduced.matches){
   stage.classList.remove('is-staged');stage.style.removeProperty('--stage-height');
   stage.dataset.scrollDistance='0';geometry=[];
   ['--field-scale-x','--field-scale-y','--field-mask-x','--field-mask-y','--wash-x','--wash-y'].forEach(name=>stage.style.removeProperty(name));
   render();return;
  }
  stage.classList.add('is-staged');
  const navHeight=header.getBoundingClientRect().height;
  const viewport=Math.max(1,window.innerHeight-navHeight);
  fieldScaleX=hero.clientWidth/Math.max(1,field.offsetWidth);
  fieldScaleY=viewport/Math.max(1,field.offsetHeight);
  distance=Math.round(Math.max(420,viewport*.95));
  start=stage.getBoundingClientRect().top+window.scrollY-navHeight;
  stage.dataset.scrollDistance=String(distance);
  stage.style.setProperty('--stage-height',(hero.offsetHeight+distance)+'px');
  const heroBox=hero.getBoundingClientRect();
  const availableWidth=window.innerWidth-Math.max(40,window.innerWidth*.084);
  const titleScale=Math.max(1,Math.min(90,window.innerWidth*.05)/parseFloat(getComputedStyle(title).fontSize));
  const textScale=Math.max(1,Math.min(24,window.innerWidth*.0155)/parseFloat(getComputedStyle(intro[0]).fontSize));
  const sloganScale=Math.max(1,Math.min(40,window.innerWidth*.026)/parseFloat(getComputedStyle(slogan).fontSize));
  const boxes=items.map(element=>element.getBoundingClientRect());
  // The same two title lines share a row in the opening, then settle into P2.
  const wordGap=parseFloat(getComputedStyle(title).fontSize)*.23;
  const combinedWidth=boxes.slice(0,lines.length).reduce((sum,box)=>sum+box.width,0)+wordGap*(lines.length-1);
  const headingScale=Math.min(titleScale,availableWidth/combinedWidth);
  const scales=items.map((element,index)=>index<lines.length?headingScale:Math.min(index<items.length-1?textScale:sloganScale,availableWidth/boxes[index].width));
  const headingHeight=Math.max(...boxes.slice(0,lines.length).map(box=>box.height))*headingScale;
  const total=headingHeight+30+boxes.slice(lines.length).reduce((sum,box,index)=>sum+box.height*scales[index+lines.length],0)+26;
  const fit=Math.max(.1,Math.min(1,(viewport-48)/total));
  let y=Math.max(24,(viewport-total*fit)/2);
  let titleX=(window.innerWidth-combinedWidth*headingScale*fit)/2;
  geometry=items.map((element,index)=>{
   const box=boxes[index],scale=scales[index]*fit;
   const initialX=index<lines.length?titleX:(window.innerWidth-box.width*scale)/2;
   const result={element,x:initialX-box.left,y:y-(box.top-heroBox.top),scale};
   if(index<lines.length){
    titleX+=(box.width+wordGap)*scale;
    if(index===lines.length-1)y+=(headingHeight+30)*fit;
   }else{
    y+=box.height*scale;
    if(index===items.length-2)y+=26*fit;
   }
   return result;
  });
  render();
 }
 function schedule(){if(frame===null)frame=requestAnimationFrame(render);}
 function remeasure(){if(resizeFrame===null)resizeFrame=requestAnimationFrame(measure);}
 window.addEventListener('scroll',schedule,{passive:true});
 window.addEventListener('resize',remeasure,{passive:true});
 reduced.addEventListener('change',remeasure);
 new ResizeObserver(remeasure).observe(hero);
 new ResizeObserver(remeasure).observe(header);
 if(document.fonts)document.fonts.ready.then(remeasure);
 measure();
}

// Native details remain keyboard-accessible while their answers expand and close gently.
document.querySelectorAll('.faq details').forEach(details=>{
 const summary=details.querySelector('summary');
 let animation=null,expanded=details.open;
 summary.addEventListener('click',event=>{
  if(contentMotion.matches){expanded=!details.open;return;}
  event.preventDefault();
  const from=details.getBoundingClientRect().height;
  if(animation)animation.cancel();
  expanded=!expanded;
  details.open=true;
  const to=expanded?details.getBoundingClientRect().height:summary.getBoundingClientRect().height
   +parseFloat(getComputedStyle(details).paddingTop)+parseFloat(getComputedStyle(details).paddingBottom)
   +parseFloat(getComputedStyle(details).borderBottomWidth);
  animation=details.animate([{height:from+'px'},{height:to+'px'}],{duration:320,easing:'cubic-bezier(.22,1,.36,1)'});
  details.style.overflow='hidden';
  const current=animation;
  animation.finished.then(()=>{
   if(animation!==current)return;details.open=expanded;details.style.removeProperty('overflow');animation=null;
  }).catch(()=>{});
 });
 contentMotion.addEventListener('change',event=>{
  if(!event.matches){expanded=details.open;return;}if(animation)animation.cancel();animation=null;details.open=expanded;details.style.removeProperty('overflow');
 });
});

// Dates, not unpublished start/cutoff times: update on UTC+8 calendar boundaries.
{
 const bar=document.querySelector('.timeline-progress');
 const status=document.querySelector('#timeline-status');
 const rail=document.querySelector('.timeline-rail');
 const marker=document.querySelector('.timeline-current');
 const tooltip=document.querySelector('#timeline-tooltip');
 const day=86400000;
 const start=Date.parse(bar.dataset.start+'T00:00:00Z');
 const deadline=Date.parse(bar.dataset.deadline+'T00:00:00Z');
 const end=Date.parse(bar.dataset.end+'T00:00:00Z');
 let lastDate='';
 function positionTooltip(){
  const bounds=rail.closest('.timeline-body').getBoundingClientRect();
  const markerBounds=marker.getBoundingClientRect();
  const width=tooltip.getBoundingClientRect().width;
  const centeredLeft=markerBounds.left+markerBounds.width/2-width/2;
  const left=Math.max(bounds.left,Math.min(centeredLeft,bounds.right-width));
  tooltip.style.setProperty('--tooltip-shift',(left-centeredLeft)+'px');
 }
 function update(){
  const date=new Date(Date.now()+8*3600000).toISOString().slice(0,10);
  if(date===lastDate)return;lastDate=date;
  const today=Date.parse(date+'T00:00:00Z');
  const progress=Math.max(0,Math.min(1,(today-start)/(end-start)));
  const percent=Math.round(progress*100);
  let label;
  if(today<start){const days=Math.round((start-today)/day);label='Starts in '+days+' '+(days===1?'day':'days');}
  else if(today===start)label='Competition begins today';
  else if(today<deadline)label='Competition dates';
  else if(today===deadline)label='Submission deadline today';
  else if(today<end){const days=Math.round((end-today)/day);label='Demo Day in '+days+' '+(days===1?'day':'days');}
  else if(today===end)label='Demo Day today';
  else label='Timeline complete';
  rail.style.setProperty('--date-progress',String(progress));
  marker.dataset.edge=progress<=.1?'start':progress>=.9?'end':'middle';
  tooltip.textContent=today<start?'The competition has not started yet.'
   :today===start?'The competition begins today.'
   :today<deadline?'The competition is in progress.'
   :today===deadline?'Submission deadline is today.'
   :today<end?'Submissions closed. Demo Day is next.'
   :today===end?'Today is Demo Day.'
   :'The competition timeline is complete.';
  bar.setAttribute('aria-valuenow',String(percent));
  bar.setAttribute('aria-valuetext',percent+'% · '+label+' · '+date+' (UTC+8)');
  bar.title='As of '+date+' (UTC+8). Calendar-date progress; exact event times to be announced.';
  status.textContent=label;positionTooltip();
 }
 marker.addEventListener('pointerenter',()=>{positionTooltip();marker.classList.remove('tooltip-dismissed');});
 marker.addEventListener('focusin',()=>{positionTooltip();marker.classList.remove('tooltip-dismissed');});
 window.addEventListener('resize',positionTooltip,{passive:true});
 document.addEventListener('keydown',event=>{if(event.key==='Escape')marker.classList.add('tooltip-dismissed');});
 update();
 setInterval(()=>{if(!document.hidden)update();},60000);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)update();});
 window.addEventListener('pageshow',update);
}
