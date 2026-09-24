import argparse, csv, json, math, re
from pathlib import Path
import fitz

NUMBER_RE = re.compile(r'^\d{3,5}$')


def clean(s):
    return ' '.join(s.replace('\xa0',' ').split()).strip()


def mid(b):
    return ((b[0]+b[2])/2, (b[1]+b[3])/2)


def dist_point_seg(px,py,x1,y1,x2,y2):
    dx,dy=x2-x1,y2-y1
    if dx==dy==0:
        return math.hypot(px-x1,py-y1),0.0
    t=((px-x1)*dx+(py-y1)*dy)/(dx*dx+dy*dy)
    t=max(0,min(1,t))
    return math.hypot(px-(x1+t*dx),py-(y1+t*dy)),t


def seg_len(l):
    return math.hypot(l['x2']-l['x1'], l['y2']-l['y1'])


def angle(l):
    return math.degrees(math.atan2(l['y2']-l['y1'], l['x2']-l['x1']))%180


def orient(a):
    if min(a,180-a)<8:return 'horizontal'
    if abs(a-90)<8:return 'vertical'
    return 'diagonal'


def endpoints(l):
    return [(l['x1'],l['y1']),(l['x2'],l['y2'])]


def endpoint_gap(a,b):
    return min(math.dist(pa,pb) for pa in endpoints(a) for pb in endpoints(b))


def axis_interval(l):
    if l['orientation']=='horizontal':
        return sorted((l['x1'],l['x2']))
    if l['orientation']=='vertical':
        return sorted((l['y1'],l['y2']))
    # diagonal: project on its own direction
    dx,dy=l['x2']-l['x1'],l['y2']-l['y1']
    L=math.hypot(dx,dy) or 1
    ux,uy=dx/L,dy/L
    return sorted((l['x1']*ux+l['y1']*uy,l['x2']*ux+l['y2']*uy))


def overlap(a,b):
    ia,ib=axis_interval(a),axis_interval(b)
    return max(0,min(ia[1],ib[1])-max(ia[0],ib[0]))


def parallel(a,b,tol=8):
    d=abs(a['angle']-b['angle'])%180
    return min(d,180-d)<=tol


def same_axis(a,b):
    return a['orientation']==b['orientation'] and a['orientation'] in ('horizontal','vertical')


def extract(page):
    texts=[]
    for block in page.get_text('dict').get('blocks',[]):
        for line in block.get('lines',[]):
            for span in line.get('spans',[]):
                t=clean(span.get('text',''))
                if t:
                    texts.append({'text':t,'bbox':list(span['bbox'])})

    drawings=[]
    lines=[]
    for di,d in enumerate(page.get_drawings()):
        drawings.append({
            'drawing_id':di,'seqno':d.get('seqno'),'type':d.get('type'),
            'width':d.get('width'),'color':d.get('color'),'items_count':len(d.get('items',[])),
            'rect':list(d['rect']) if d.get('rect') else None,
        })
        for ii,item in enumerate(d.get('items',[])):
            if item[0] != 'l':
                continue
            p1,p2=item[1],item[2]
            l={'line_id':len(lines),'drawing_id':di,'item_id':ii,'seqno':d.get('seqno'),
               'x1':float(p1.x),'y1':float(p1.y),'x2':float(p2.x),'y2':float(p2.y),
               'width':d.get('width'),'items_count':len(d.get('items',[]))}
            l['length']=seg_len(l); l['angle']=angle(l); l['orientation']=orient(l['angle'])
            if l['length']>=1.5:
                lines.append(l)

    candidates=[]
    for t in texts:
        s=t['text']
        if not NUMBER_RE.fullmatch(s):
            continue
        v=int(s)
        if v<50 or v>50000 or v==4116:
            continue
        cx,cy=mid(t['bbox'])
        opts=[]
        for L in lines:
            d,_=dist_point_seg(cx,cy,L['x1'],L['y1'],L['x2'],L['y2'])
            if d>14: continue
            score=0; reasons=[]
            w=L['width'] if isinstance(L['width'],(int,float)) else 9
            if w<=0.5: score+=3; reasons.append('thin')
            if L['items_count']<=5: score+=1; reasons.append('small_drawing_group')
            if d<=8: score+=2; reasons.append('close_text')
            siblings=[x for x in lines if x['drawing_id']==L['drawing_id']]
            if len(siblings)>=2:
                shorts=sum(1 for x in siblings if x['length']<0.45*L['length'])
                if shorts: score+=2; reasons.append('arrow_or_extension_pattern')
            if L['length']>=max(8,1.8*(t['bbox'][2]-t['bbox'][0])):
                score+=1; reasons.append('line_longer_than_text')
            opts.append({'line_id':L['line_id'],'distance':round(d,2),'score':score,'reasons':reasons})
        opts.sort(key=lambda z:(-z['score'],z['distance']))
        best=opts[0] if opts else None
        candidates.append({
            'id':len(candidates),'value':v,'bbox':t['bbox'],'center':[cx,cy],
            'dimension_line_id':best['line_id'] if best and best['score']>=4 else None,
            'dimension_confidence_score':best['score'] if best else 0,
            'association_distance':best['distance'] if best else None,
            'association_reasons':best['reasons'] if best else [],
            'alternatives':opts[:5],
        })

    # Build dimension graph.
    accepted=[c for c in candidates if c['dimension_line_id'] is not None]
    by_id={c['id']:c for c in accepted}
    edges=[]
    for c in candidates:
        c['graph_relations']=[]
        c['graph_component']=None
        c['classification']='isolated'

    for i,a in enumerate(accepted):
        la=lines[a['dimension_line_id']]
        for b in accepted[i+1:]:
            lb=lines[b['dimension_line_id']]
            gap=endpoint_gap(la,lb)
            ov=overlap(la,lb)
            rel=None
            strength=0
            if gap<=4:
                # A chain connection is allowed even when orientation changes.
                rel='endpoint_connected'
                strength=1.0
            elif parallel(la,lb) and same_axis(la,lb):
                shorter=min(la['length'],lb['length'])
                overlap_ratio=ov/shorter if shorter else 0
                if overlap_ratio>=0.55:
                    rel='parallel_overlap'
                    strength=overlap_ratio
                elif gap<=25:
                    rel='parallel_nearby'
                    strength=max(0,1-gap/25)
            if rel:
                edge={'a':a['id'],'b':b['id'],'a_value':a['value'],'b_value':b['value'],
                      'relation':rel,'endpoint_gap':round(gap,2),'overlap':round(ov,2),
                      'strength':round(strength,3)}
                edges.append(edge)
                a['graph_relations'].append(edge)
                b['graph_relations'].append(edge)

    # Connected components over the dimension graph.
    adj={c['id']:set() for c in accepted}
    for e in edges:
        adj[e['a']].add(e['b']); adj[e['b']].add(e['a'])
    comp_id=0
    for start in adj:
        if by_id[start]['graph_component'] is not None:
            continue
        stack=[start]; by_id[start]['graph_component']=comp_id
        while stack:
            u=stack.pop()
            for v in adj[u]:
                if by_id[v]['graph_component'] is None:
                    by_id[v]['graph_component']=comp_id; stack.append(v)
        comp_id+=1

    # Classify each dimension based on graph evidence.
    for c in candidates:
        if c['dimension_line_id'] is None:
            c['classification']='unlinked_number'
            continue
        rels=c['graph_relations']
        if not rels:
            c['classification']='independent_candidate'
            continue
        has_chain=any(e['relation']=='endpoint_connected' for e in rels)
        has_overlap=any(e['relation']=='parallel_overlap' for e in rels)
        if has_overlap and has_chain:
            c['classification']='chain_with_overlapping_detail'
        elif has_overlap:
            c['classification']='overlapping_detail_candidate'
        elif has_chain:
            c['classification']='dimension_chain_member'
        else:
            c['classification']='related_dimension'

    # For overlap pairs, identify the shorter dimension as a likely detail, but
    # explicitly keep this as a hypothesis, never as a deletion rule.
    overlap_pairs=[]
    for e in edges:
        if e['relation']!='parallel_overlap': continue
        a=by_id[e['a']]; b=by_id[e['b']]
        la=lines[a['dimension_line_id']]; lb=lines[b['dimension_line_id']]
        if la['length']<lb['length']:
            detail,overall=a,b
        else:
            detail,overall=b,a
        hypothesis={'detail_id':detail['id'],'detail_value':detail['value'],
                    'overall_id':overall['id'],'overall_value':overall['value'],
                    'reason':'shorter parallel dimension with substantial overlap',
                    'confidence':e['strength']}
        overlap_pairs.append(hypothesis)

    graph={
        'nodes':[{
            'id':c['id'],'value':c['value'],'line_id':c['dimension_line_id'],
            'component':c['graph_component'],'classification':c['classification']
        } for c in accepted],
        'edges':edges,
        'overlap_hypotheses':overlap_pairs,
        'component_count':comp_id
    }
    return texts,drawings,lines,candidates,graph


def annotate(page,candidates,lines,graph):
    # All dimension candidates: red. Linked dimension candidates: green.
    for c in candidates:
        b=fitz.Rect(*c['bbox'])
        linked=c['dimension_line_id'] is not None
        col=(0,0.7,0) if linked else (1,0,0)
        page.draw_rect(b,color=col,width=1.1)
        if linked:
            L=lines[c['dimension_line_id']]
            page.draw_line(fitz.Point(L['x1'],L['y1']),fitz.Point(L['x2'],L['y2']),color=(0,0,1),width=0.9)
            lab=fitz.Rect(b.x0,b.y0-9,b.x0+90,b.y0)
            page.insert_textbox(lab,f"{c['id']}:{c['value']} [{c['classification']}]",fontsize=4.5,color=col)
        else:
            lab=fitz.Rect(b.x0,b.y0-9,b.x0+40,b.y0)
            page.insert_textbox(lab,f"{c['id']}:{c['value']}",fontsize=4.5,color=col)

    # Draw graph links in orange.
    for e in graph['edges']:
        a=next(c for c in candidates if c['id']==e['a'])
        b=next(c for c in candidates if c['id']==e['b'])
        la,lb=lines[a['dimension_line_id']],lines[b['dimension_line_id']]
        # closest endpoints
        pairs=[(math.dist(pa,pb),pa,pb) for pa in endpoints(la) for pb in endpoints(lb)]
        _,pa,pb=min(pairs,key=lambda x:x[0])
        color=(1,0.5,0)
        page.draw_line(fitz.Point(*pa),fitz.Point(*pb),color=color,width=0.7)


def run(src,out):
    doc=fitz.open(src)
    report={'source':str(src),'pages':[]}
    for pn,page in enumerate(doc,1):
        texts,drawings,lines,candidates,graph=extract(page)
        annotate(page,candidates,lines,graph)
        report['pages'].append({
            'page':pn,'text_count':len(texts),'drawing_count':len(drawings),'vector_line_count':len(lines),
            'candidates':candidates,'drawings':drawings,'lines':lines,'dimension_graph':graph
        })
    out.mkdir(parents=True,exist_ok=True)
    doc.save(out/'dimension_graph_v05_debug.pdf')
    (out/'dimension_graph_v05.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'dimension_graph.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f); w.writerow(['id','value','line_id','component','classification','relations'])
        for p in report['pages']:
            for c in p['candidates']:
                if c['dimension_line_id'] is None: continue
                rel='; '.join(f"{e['relation']}:{e['a_value']}-{e['b_value']}" for e in c['graph_relations'])
                w.writerow([c['id'],c['value'],c['dimension_line_id'],c['graph_component'],c['classification'],rel])
    return report


if __name__=='__main__':
    ap=argparse.ArgumentParser(description='Dimension graph prototype v0.5')
    ap.add_argument('pdf',type=Path)
    ap.add_argument('-o','--out',type=Path,default=Path('result_v05'))
    args=ap.parse_args()
    r=run(args.pdf,args.out)
    for p in r['pages']:
        print(f"PAGE {p['page']}: lines={p['vector_line_count']} candidates={len(p['candidates'])} components={p['dimension_graph']['component_count']}")
        print('  nodes:', [(n['value'],n['classification'],n['component']) for n in p['dimension_graph']['nodes']])
        print('  edges:', [(e['a_value'],e['relation'],e['b_value'],e['endpoint_gap'],e['overlap']) for e in p['dimension_graph']['edges']])
        print('  overlap hypotheses:', p['dimension_graph']['overlap_hypotheses'])


        print()
