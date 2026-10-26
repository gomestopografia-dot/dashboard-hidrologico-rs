"""Integração experimental com consulta fixa Tags_data, conforme documentação pública Defesa Civil RS."""
from urllib.request import Request, urlopen
from html.parser import HTMLParser
import json, ssl, re, time, threading, unicodedata

DOC='https://sistemas.defesacivil.rs.gov.br/api-redehidrometeorologica'
API='https://redehidrometeorologica.defesacivil.rs.gov.br/graphql'
_cache={'at':0,'stations':None,'error':None};_lock=threading.Lock()

class Snippets(HTMLParser):
 def __init__(self):super().__init__();self.active=0;self.buf=[];self.parts=[]
 def handle_starttag(self,tag,attrs):
  if tag in ('pre','code'):
   if self.active==0:self.buf=[]
   self.active+=1
 def handle_endtag(self,tag):
  if tag in ('pre','code') and self.active:
   self.active-=1
   if self.active==0:self.parts.append(''.join(self.buf))
 def handle_data(self,data):
  if self.active:self.buf.append(data)

def query_from_guide():
 req=Request(DOC,headers={'User-Agent':'Mozilla/5.0 HidroRS/8.2'})
 with urlopen(req,timeout=15,context=ssl.create_default_context()) as r:page=r.read(1_000_000).decode('utf-8','replace')
 p=Snippets();p.feed(page)
 for s in p.parts:
  if 'query Tags_data' in s and 'qualle_meteorologia' in s and 'rio_nivel_tendencia' in s:
   s=s[s.index('query Tags_data'):].strip().strip('`')
   depth=0
   for i,ch in enumerate(s):
    if ch=='{':depth+=1
    elif ch=='}':
     depth-=1
     if depth==0:return s[:i+1]
 raise ValueError('Consulta oficial não localizada no guia')

def normalize(s):
 s=unicodedata.normalize('NFKD',str(s or '').lower())
 return ''.join(c for c in s if not unicodedata.combining(c))

def val(x):
 return x.get('value') if isinstance(x,dict) else x

def numeric(x):
 try:
  if x is None or isinstance(x,bool):return None
  v=float(str(x).replace(',','.'))
  return v if -1000<v<10000 else None
 except (TypeError,ValueError):return None

def read():
 with _lock:
  if time.time()-_cache['at']<300:return _cache['stations'],_cache['error']
  try:
   q=query_from_guide()
   body=json.dumps({'query':q,'operationName':'Tags_data'}).encode()
   req=Request(API,data=body,headers={'Content-Type':'application/json','Accept':'application/json','User-Agent':'HidroRS/8.2'},method='POST')
   with urlopen(req,timeout=20,context=ssl.create_default_context()) as res:data=json.load(res)
   if data.get('errors'):raise ValueError(str(data['errors'])[:350])
   items=data['data']['tags_data']['qualle_meteorologia']
   if not isinstance(items,list):raise ValueError('Resposta sem lista de estações')
   _cache.update(at=time.time(),stations=items,error=None)
  except Exception as e:
   _cache.update(at=time.time(),stations=None,error=f'{type(e).__name__}: {str(e)[:250]}')
  return _cache['stations'],_cache['error']

def matches(item,river,city):
 names=item.get('name') or {}
 label=' '.join(str(names.get(k) or '') for k in ('prefix','general','local'))
 river_label=val(((item.get('data') or {}).get('rio') or {}).get('rio_nome'))
 hay=normalize(label+' '+str(river_label))
 if normalize(river) not in hay:return False
 citykey=normalize(city)
 if citykey in hay:return True
 if city=='Santa Maria' and 'passo do verde' in hay:return True
 if city=='Arroio do Meio' and ('ers-130' in hay or 'ers 130' in hay):return True
 return False

def candidate_summary(items,river,city):
 return [{'codigo':i.get('codigo'),'nome':' '.join(str((i.get('name') or {}).get(k) or '') for k in ('prefix','general','local')),'rio':val(((i.get('data') or {}).get('rio') or {}).get('rio_nome'))} for i in items if matches(i,river,city)]


def same_station_code(actual,expected):
 """Compara códigos com zeros e hífens opcionais, sem correspondência aproximada."""
 def key(v):
  s=re.sub(r'[^A-Z0-9]','',str(v or '').upper())
  m=re.fullmatch(r'DCRS0*(\d+)',s)
  return ('DCRS',int(m.group(1))) if m else ('OTHER',s)
 return bool(actual and expected) and key(actual)==key(expected)
