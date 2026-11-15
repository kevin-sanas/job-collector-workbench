"""Read displayed digits by local glyph comparison, without network requests."""
import json


def accept_digit_matches(results):
    mapping = {}
    for codepoint, candidates in results.items():
        if len(candidates) < 2:
            continue
        best, second = candidates[0], candidates[1]
        if best['score'] >= .95 and best['score'] - second['score'] >= .1 and best['digit'] in '0123456789':
            mapping[int(codepoint)] = best['digit']
    if len(set(mapping.values())) != len(mapping):
        return {}
    return mapping


GLYPH_SCRIPT = '''
const element=document.querySelector(arguments[0]);if(!element)return {};
const family=getComputedStyle(element).fontFamily;
const canvas=document.createElement('canvas');canvas.width=300;canvas.height=300;
const ctx=canvas.getContext('2d',{willReadFrequently:true});ctx.font='200px '+family;ctx.textBaseline='alphabetic';
const normalized=char=>{
    ctx.clearRect(0,0,300,300);ctx.fillText(char,30,240);
    const pixels=ctx.getImageData(0,0,300,300).data;
    let left=300,top=300,right=0,bottom=0;
    for(let y=0;y<300;y++)for(let x=0;x<300;x++)if(pixels[(y*300+x)*4+3]>100){
        left=Math.min(left,x);right=Math.max(right,x);top=Math.min(top,y);bottom=Math.max(bottom,y);}
    if(right<left||bottom<top)return null;
    const crop=document.createElement('canvas');crop.width=64;crop.height=96;
    const small=crop.getContext('2d',{willReadFrequently:true});
    small.drawImage(canvas,left,top,right-left+1,bottom-top+1,0,0,64,96);
    const values=small.getImageData(0,0,64,96).data;
    return Array.from({length:6144},(_,i)=>values[i*4+3]>100);
};
const reference=Array.from('0123456789',digit=>[digit,normalized(digit)]);
if(reference.some(([,mask])=>!mask))return {};
const result={};
for(const code of JSON.parse(arguments[1])){
    const sample=normalized(String.fromCharCode(code));if(!sample)continue;
    result[code]=reference.map(([digit,mask])=>{
        let intersection=0,union=0;
        for(let i=0;i<sample.length;i++){if(sample[i]&&mask[i])intersection++;if(sample[i]||mask[i])union++;}
        return {digit,score:union?intersection/union:0};
    }).sort((a,b)=>b.score-a.score).slice(0,2);
}
return result;
'''


class DisplayFontDecoder:
    def __init__(self, selector=''):
        self.selector = selector
        self.cache = {}

    def decode(self, page, text):
        codes = sorted({ord(char) for char in text if 0xE000 <= ord(char) <= 0xF8FF})
        if not codes or not self.selector:
            return text
        key = (page.tab_id, page.url, tuple(codes))
        if key not in self.cache:
            self.cache[key] = {}
            try:
                self.cache[key] = accept_digit_matches(page.run_js(GLYPH_SCRIPT, self.selector, json.dumps(codes)) or {})
            except Exception:
                pass
        return text.translate(self.cache[key])
