import fitz, json, math, re, csv
from pathlib import Path

NUMBER_RE = re.compile(r'^\d{3,5}$')


def clean(s): return ' '.join(s.replace('\xa0',' ').split()).strip()
def mid(b): return ((b[0]+b[2])/2,(b[1]+b[3])/2)
def dist_point_seg(px,py,x1,y1,x2,y2):
    dx,dy=x2-x1,y2-y1
    if dx==dy==0:return math.hypot(px-x1,py-y1),0
    t=((px-x1)*dx+(py-y1)*dy)/(dx*dx+dy*dy); t=max(0,min(1,t))
    return math.hypot(px-(x1+t*dx),py-(y1+t*dy)),t
def length(x1,y1,x2,y2): return math.hypot(x2-x1,y2-y1)
def angle(x1,y1,x2,y2): return math.degrees(math.atan2(y2-y1,x2-x1))%180
def orient(a):
    if min(a,180-a)<8:return 'horizontal'
    if abs(a-90)<8:return 'vertical'
    return 'diagonal'
def endpoint_gap(a,b):
    pa=[(a['x1'],a['y1']),(a['x2'],a['y2'])]
    pb=[(b['x1'],b['y1']),(b['x2'],b['y2'])]
    return min(math.hypot(x-u,y-v) for x,y in pa for u,v in pb)


def extract(page):
    texts=[]
    for block in page.get_text('dict').get('blocks',[]):
      for line in block.get('lines',[]):
       for span in line.get('spans',[]):
        t=clean(span.get('text',''))
        if t:texts.append({'text':t,'bbox':list(span['bbox'])})

    drawings=[]; lines=[]
    for di,d in enumerate(page.get_drawings()):
        meta={
            'drawing_id':di,'seqno':d.get('seqno'),'type':d.get('type'),
            'width':d.get('width'),'color':d.get('color'),'items_count':len(d.get('items',[])),
            'rect':list(d['rect']) if d.get('rect') else None,
        }
        drawings.append(meta)
        for ii,item in enumerate(d.get('items',[])):
            if item[0]!='l':continue
            p1,p2=item[1],item[2]
            x1,y1,x2,y2=p1.x,p1.y,p2.x,p2.y
            L=length(x1,y1,x2,y2)
            if L<1.5:continue
            a=angle(x1,y1,x2,y2)
            lines.append({'line_id':len(lines),'drawing_id':di,'item_id':ii,'seqno':d.get('seqno'),
                          'x1':x1,'y1':y1,'x2':x2,'y2':y2,'length':L,'angle':a,
                          'orientation':orient(a),'width':d.get('width'),'items_count':len(d.get('items',[]))})

    candidates=[]
    for t in texts:
      s=t['text']
      if not NUMBER_RE.fullmatch(s):continue
      v=int(s)
      if v<50 or v>50000 or v==4116:continue
      cx,cy=mid(t['bbox'])
      ranked=[]
      for L in lines:
        d,tp=dist_point_seg(cx,cy,L['x1'],L['y1'],L['x2'],L['y2'])
        if d<=14:ranked.append((d,L))
      ranked.sort(key=lambda z:z[0])
      # evaluate each nearby line using its drawing style/group
      opts=[]
      for d,L in ranked[:12]:
        score=0
        reasons=[]
        w=L['width'] if isinstance(L['width'],(int,float)) else 9
        if w<=0.5: score+=3; reasons.append('thin')
        if L['items_count']<=5: score+=1; reasons.append('small_drawing_group')
        if d<=8: score+=2; reasons.append('close_text')
        # A dimension drawing often contains another short segment at an endpoint.
        siblings=[x for x in lines if x['drawing_id']==L['drawing_id']]
        if len(siblings)>=2:
            shorts=sum(1 for x in siblings if x['length'] < 0.45*L['length'])
            if shorts: score+=2; reasons.append('arrow_or_extension_pattern')
        if L['length'] >= max(8, 1.8*(t['bbox'][2]-t['bbox'][0])):
            score+=1; reasons.append('line_longer_than_text')
        opts.append({'line_id':L['line_id'],'distance':round(d,2),'score':score,'reasons':reasons})
      opts.sort(key=lambda x:(-x['score'],x['distance']))
      best=opts[0] if opts else None
      c={'id':len(candidates),'value':v,'bbox':t['bbox'],'center':[cx,cy],
         'dimension_line_id':best['line_id'] if best and best['score']>=4 else None,
         'dimension_confidence_score':best['score'] if best else 0,
         'association_distance':best['distance'] if best else None,
         'association_reasons':best['reasons'] if best else [],
         'alternatives':opts[:5]}
      candidates.append(c)

    # Relations among accepted dimension lines
    for c in candidates:c['relations']=[]
    accepted=[c for c in candidates if c['dimension_line_id'] is not None]
    for i,a in enumerate(accepted):
      la=lines[a['dimension_line_id']]
      for b in accepted[i+1:]:
        lb=lines[b['dimension_line_id']]
        if la['orientation']!=lb['orientation']:continue
        gap=endpoint_gap(la,lb)
        if gap<=3:
          rel='endpoint_connected'
        elif la['orientation'] in ('horizontal','vertical'):
          if la['orientation']=='vertical':
            ia=sorted((la['y1'],la['y2'])); ib=sorted((lb['y1'],lb['y2']))
            overlap=max(0,min(ia[1],ib[1])-max(ia[0],ib[0]))
          else:
            ia=sorted((la['x1'],la['x2'])); ib=sorted((lb['x1'],lb['x2']))
            overlap=max(0,min(ia[1],ib[1])-max(ia[0],ib[0]))
          shorter=min(la['length'],lb['length'])
          if overlap>0.2*shorter: rel='parallel_overlapping'
          elif gap<20: rel='parallel_nearby'
          else: continue
        else: continue
        a['relations'].append({'other_id':b['id'],'other_value':b['value'],'relation':rel,'gap':round(gap,2)})
        b['relations'].append({'other_id':a['id'],'other_value':a['value'],'relation':rel,'gap':round(gap,2)})
    return texts,drawings,lines,candidates


def annotate(page,candidates,lines):
    for c in candidates:
      b=fitz.Rect(*c['bbox'])
      col=(0,0.8,0) if c['dimension_line_id'] is not None else (1,0,0)
      page.draw_rect(b,color=col,width=1.2)
      if c['dimension_line_id'] is not None:
        L=lines[c['dimension_line_id']]
        page.draw_line(fitz.Point(L['x1'],L['y1']),fitz.Point(L['x2'],L['y2']),color=(0,0,1),width=1.0)
      lab=fitz.Rect(b.x0,b.y0-9,b.x0+50,b.y0)
      page.insert_textbox(lab,f"{c['id']}:{c['value']}",fontsize=5,color=col)


def run(src,out):
    doc=fitz.open(src); report={'source':str(src),'pages':[]}
    for pn,page in enumerate(doc,1):
      texts,drawings,lines,candidates=extract(page); annotate(page,candidates,lines)
      report['pages'].append({'page':pn,'text_count':len(texts),'drawing_count':len(drawings),'vector_line_count':len(lines),
                              'candidates':candidates,'drawings':drawings,'lines':lines})
    out.mkdir(parents=True,exist_ok=True)
    doc.save(out/'geometry_v04_debug.pdf')
    (out/'geometry_v04.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'dimension_line_candidates.csv').open('w',newline='',encoding='utf-8-sig') as f:
      w=csv.writer(f);w.writerow(['id','value','dimension_line_id','score','distance','reasons','relations'])
      for p in report['pages']:
       for c in p['candidates']:
        w.writerow([c['id'],c['value'],c['dimension_line_id'],c['dimension_confidence_score'],c['association_distance'],','.join(c['association_reasons']),';'.join(f"{r['relation']}:{r['other_value']}" for r in c['relations'])])
    return report

if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('pdf',type=Path);ap.add_argument('-o','--out',type=Path,default=Path('result_v04'))
    a=ap.parse_args();r=run(a.pdf,a.out)
    for p in r['pages']:
      print('PAGE',p['page'])
      for c in p['candidates']:
        print(c['id'],c['value'],'line=',c['dimension_line_id'],'score=',c['dimension_confidence_score'],'rels=',c['relations'])
