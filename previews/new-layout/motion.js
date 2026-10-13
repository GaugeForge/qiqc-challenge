// Rabi model at full opacity; only the darkest green stop is softened.
const canvas=document.querySelector('#field'),ctx=canvas.getContext('2d',{alpha:false});
const field=document.querySelector('.field'),button=document.querySelector('.motion');
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
const palette=[[245,247,248],[153,217,208],[35,142,133],[57,101,92],[250,171,113]];
const lut=new Uint8ClampedArray(3072);
for(let i=0;i<1024;i++){const v=i/1023*4,k=Math.min(3,Math.floor(v)),f=v-k;for(let c=0;c<3;c++)lut[i*3+c]=palette[k][c]*(1-f)+palette[k+1][c]*f;}
let width,height,pixels,phase=0,last=0,paint=0,frame=null,playing=!reduced.matches;
function draw(){const a=1+.3*Math.sin(phase*Math.PI/10),aa=a*a,data=pixels.data;
for(let row=0;row<height;row++){const d=2.4-4.8*row/(height-1),q=aa+d*d,s=aa/q,w=Math.PI*Math.sqrt(q)*3.5/(width-1);for(let col=0;col<width;col++){const wave=Math.sin(w*col),color=Math.round(s*wave*wave*1023)*3,j=(row*width+col)*4;data[j]=lut[color];data[j+1]=lut[color+1];data[j+2]=lut[color+2];data[j+3]=255;}}
ctx.putImageData(pixels,0,0);field.classList.add('ready');}
function tick(now){frame=null;if(document.hidden||!playing){last=0;return;}const dt=last?Math.min((now-last)/1000,.08):0;phase+=dt;last=now;if(now-paint>=1000/24){draw();paint=now;}frame=requestAnimationFrame(tick);}
function schedule(){if(frame===null&&playing&&!document.hidden)frame=requestAnimationFrame(tick);}
function resize(){width=Math.min(1440,Math.max(480,Math.round(field.clientWidth)));height=Math.max(220,Math.round(width*field.clientHeight/field.clientWidth));canvas.width=width;canvas.height=height;pixels=ctx.createImageData(width,height);draw();schedule();}
function sync(){button.textContent=playing?'Ⅱ':'▷';button.setAttribute('aria-label',playing?'Pause background animation':'Play background animation');button.setAttribute('aria-pressed',String(!playing));last=0;schedule();}
button.addEventListener('click',()=>{playing=!playing;sync();});reduced.addEventListener('change',e=>{playing=!e.matches;sync();});document.addEventListener('visibilitychange',()=>{last=0;schedule();});new ResizeObserver(resize).observe(field);resize();sync();
