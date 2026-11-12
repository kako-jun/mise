// Temporary, pixel-like calico cat; detailed character design remains undecided.
const cat = document.querySelector('#walking-cat');
const motion = matchMedia('(prefers-reduced-motion: reduce)');
const metricsContext = document.createElement('canvas').getContext('2d');
const selectors = '.logo-caption, .category a, .category .sub, #breadcrumbs, .site-content a, .site-content p, #result-title, .picks h2';
let platforms=[], from=null, target=null, state='hidden', started=0, duration=0, animation=0, dirty=true, x=0, y=0, walkEnd=0;
const random=(min,max)=>min+Math.random()*(max-min);
function measure() {
  const found=[];
  for(const el of document.querySelectorAll(selectors)) {
    if(!el.getClientRects().length || !el.textContent.trim()) continue;
    const range=document.createRange(); range.selectNodeContents(el);
    metricsContext.font=getComputedStyle(el).font;
    const m=metricsContext.measureText(el.textContent.trim());
    const offset=Math.max(0,Math.min(4,(m.fontBoundingBoxAscent||m.actualBoundingBoxAscent)-m.actualBoundingBoxAscent));
    for(const r of range.getClientRects()) {
      if(r.width<14 || r.height<3 || r.top<24 || r.top>innerHeight-10 || r.right<20 || r.left>innerWidth-20) continue;
      found.push({x:Math.max(10,Math.min(innerWidth-10,r.left+Math.min(r.width/2,40))),y:r.top+offset,width:r.width});
    }
  }
  platforms=found; dirty=false;
}
function phase(next,now,ms) { state=next; started=now; duration=ms; cat.dataset.phase=next; }
function reset() {
  dirty=true;
  if(state!=='hidden' && state!=='retire' && state!=='dormant') phase('retire',performance.now(),300);
  start();
}
function chooseTarget() {
  const choices=platforms.filter(p=>p!==from && Math.abs(p.y-from.y)>4 && Math.abs(p.y-from.y)<110 && Math.abs(p.x-from.x)<120);
  return choices.length ? choices[Math.floor(Math.random()*choices.length)] : null;
}
function draw(opacity=1) {
  cat.removeAttribute('hidden'); cat.style.opacity=String(opacity);
  cat.style.transform=`translate(${Math.round(x-10)}px, ${Math.floor(y-21)}px) scaleX(${target && target.x<x ? -1 : 1})`;
}
function loop(now) {
  animation=0;
  if(document.hidden || motion.matches) { cat.setAttribute('hidden',''); state='hidden'; dirty=true; return; }
  if(state==='retire') {
    const t=Math.min(1,(now-started)/duration); draw(1-t);
    if(t>=1) { cat.setAttribute('hidden',''); phase('dormant',now,500); from=null; target=null; }
  } else if(state==='dormant') {
    if(now-started>=duration) state='hidden';
  } else if(state==='hidden') {
    if(dirty) measure();
    const viable=platforms.filter(p=>platforms.some(q=>q!==p && Math.abs(q.y-p.y)>4 && Math.abs(q.y-p.y)<110 && Math.abs(q.x-p.x)<120));
    if(viable.length) { from=viable[Math.floor(Math.random()*viable.length)]; x=from.x; y=from.y; phase('appear',now,300); draw(0); }
  } else if(state==='appear') {
    const t=Math.min(1,(now-started)/duration); draw(t);
    if(t>=1) phase('wait',now,random(300,1100));
  } else if(state==='wait') {
    draw();
    if(now-started>=duration) {
      target=chooseTarget();
      if(!target) phase('retire',now,300);
      else { const direction=target.x<x?-1:1; walkEnd=x+Math.min(random(3,10),from.width/4)*direction; phase('walk',now,random(400,1000)); }
    }
  } else if(state==='walk') {
    const t=Math.min(1,(now-started)/duration); x=from.x+(walkEnd-from.x)*t; y=from.y; cat.dataset.platformTop=String(from.y); draw();
    if(t>=1) phase('jump',now,random(2200,3000));
  } else if(state==='jump') {
    const t=Math.min(1,(now-started)/duration); x=walkEnd+(target.x-walkEnd)*t; y=from.y+(target.y-from.y)*t-Math.sin(Math.PI*t)*randomArc; draw();
    if(t>=1) { from=target; target=null; x=from.x; y=from.y; phase('wait',now,random(300,1100)); randomArc=random(18,32); }
  }
  animation=requestAnimationFrame(loop);
}
let randomArc=random(18,32);
function start() { if(!animation && !document.hidden && !motion.matches) animation=requestAnimationFrame(loop); else if(motion.matches) cat.setAttribute('hidden',''); }
window.addEventListener('scroll',reset,{passive:true}); window.addEventListener('resize',reset);
document.addEventListener('visibilitychange',()=>{reset();if(!document.hidden)start();});
motion.addEventListener('change',()=>{reset();start();});
new MutationObserver(reset).observe(document.querySelector('main'),{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['hidden']});
start();
