// Shared navigation and gentle link motion for all public pages.
const menu=document.querySelector('#menu-toggle');
const nav=document.querySelector('#navigation');
function setMenu(open){menu.setAttribute('aria-expanded',String(open));menu.setAttribute('aria-label',open?'Close navigation':'Open navigation');menu.title=open?'Close navigation':'Open navigation';menu.querySelector('img').src=open?'assets/x.svg':'assets/menu.svg';nav.classList.toggle('open',open);}
menu.addEventListener('click',()=>setMenu(menu.getAttribute('aria-expanded')!=='true'));
nav.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>setMenu(false)));
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&menu.getAttribute('aria-expanded')==='true'){setMenu(false);menu.focus();}});

// Reference GFdemo1: optical movement on inner surfaces keeps hit areas and scroll geometry stable.
{
 const motion=matchMedia('(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)');
 const selector='.hero-slogan mark, .basic-label, .timeline-event, .entry-timeline time, .entry-details h3, .prize-overview h3, .task-copy h3, .section-heading h2';
 const items=[...document.querySelectorAll(selector)]
  .filter(target=>!target.closest('.entry-who, .entry-how, .prize-overview, .entry-timeline'))
  .map(target=>{
   const surface=document.createElement('span');surface.className='ui-motion-surface';
   while(target.firstChild)surface.append(target.firstChild);
   target.append(surface);target.classList.add('ui-motion-target');return {target,surface};
  });
 // Move link text only, preserving each control's hit area and icon alignment.
 document.querySelectorAll('a[href]:not(.brand):not(.skip), button.button, .faq summary').forEach(target=>{
  [...target.childNodes].filter(node=>node.nodeType===Node.TEXT_NODE&&node.textContent.trim()).forEach(node=>{
   const surface=document.createElement('span');surface.className='ui-motion-surface';
   node.before(surface);surface.append(node);target.classList.add('ui-motion-target');
   items.push({target,surface});
  });
 });
 let frame=null,selecting=false;
 const pending=new Map();
 function resetItem(item){pending.delete(item);item.target.classList.remove('is-tracking');item.surface.style.removeProperty('--ui-x');item.surface.style.removeProperty('--ui-y');}
 function reset(){if(frame!==null)cancelAnimationFrame(frame);frame=null;items.forEach(resetItem);}
 function render(){
  frame=null;if(!motion.matches||selecting)return;
  for(const [item,point] of pending){
   const box=item.target.getBoundingClientRect();
   const x=Math.max(-1,Math.min(1,(point.x-box.left)/box.width*2-1));
   const y=Math.max(-1,Math.min(1,(point.y-box.top)/box.height*2-1));
   item.surface.style.setProperty('--ui-x',(x*4)+'px');item.surface.style.setProperty('--ui-y',(y*2.4)+'px');
   item.target.classList.add('is-tracking');
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
 document.addEventListener('pointerdown',()=>{selecting=true;reset();});
 window.addEventListener('pointerup',()=>{selecting=false;});
 window.addEventListener('pointercancel',()=>{selecting=false;reset();});
 window.addEventListener('scroll',reset,{passive:true});
 window.addEventListener('blur',()=>{selecting=false;reset();});
 document.addEventListener('visibilitychange',()=>{if(document.hidden){selecting=false;reset();}});
 motion.addEventListener('change',reset);
}

