from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs
from urllib.error import URLError
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
import json, ssl, os, sys, threading, time, re
import defesa_civil

BASE=Path(__file__).resolve().parent
try:
 TZ=ZoneInfo('America/Sao_Paulo')
except Exception:
 TZ=timezone(timedelta(hours=-3))  # fallback Windows sem tzdata
SOURCES={
 'Guaíba — Porto Alegre': {'url':'https://nivelguaiba.com.br/portoalegre.json','lat':-30.0346,'lon':-51.2177,'river':'Guaíba','city':'Porto Alegre','source':'Nível Guaíba (agregador; dados primários ANA/SGB)','code':'guaiba-portoalegre'},
}
CONFIG=BASE/'stations.json'
if CONFIG.exists():
 try:
  for item in json.loads(CONFIG.read_text(encoding='utf-8')):
   if all(k in item for k in ('name','lat','lon','river','city')) and (item.get('url') is None or str(item.get('url')).startswith('https://')):
    SOURCES[item['name']]={**item,'code':item.get('code',re.sub(r'[^a-z0-9]+','-',item['name'].lower())),'source':item.get('source','Feed externo configurado')}
 except Exception as exc: print('Configuração inválida:',exc)
try:
 import truststore
 TLS=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
 TLS_NAME='Windows/truststore'
except ImportError:
 TLS=ssl.create_default_context()
 TLS_NAME='Python/system'
CACHE={}; LOCK=threading.Lock(); TTL=300

def get_json(url):
 req=Request(url,headers={'User-Agent':'HidroRS/2.0 (monitoramento informativo)','Accept':'application/json'})
 with urlopen(req,timeout=15,context=TLS) as res:
  if int(res.headers.get('Content-Length','0'))>5_000_000:raise ValueError('Resposta muito grande')
  raw=res.read(5_000_001)
  if len(raw)>5_000_000:raise ValueError('Resposta muito grande')
  return json.loads(raw)

def parse_time(s):
 try:
  d=datetime.fromisoformat(str(s).replace('Z','+00:00'))
  return d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ)
 except (ValueError,TypeError):return None

def points_from_json(data):
 # Formato documentado das séries públicas: {"YYYY-MM-DD HH:MM": nível_em_metros}
 if isinstance(data,dict) and isinstance(data.get('series'),list):data=data['series']
 pts=[]
 if isinstance(data,dict):
  for k,v in data.items():
   t=parse_time(k)
   if t:
    try:
     level=float(v)
     if 0<level<100:pts.append((t,level))
    except (TypeError,ValueError):pass
 elif isinstance(data,list):
  for obj in data:
   if not isinstance(obj,dict):continue
   t=parse_time(obj.get('timestamp') or obj.get('time') or obj.get('date'))
   try:
    v=float(obj.get('level',obj.get('nivel')))
    if t and 0<v<100:pts.append((t,v))
   except (TypeError,ValueError):pass
 return sorted(dict(pts).items())

def delta(pts,h):
 if not pts:return None
 target=pts[-1][0]-timedelta(hours=h)
 candidates=[p for p in pts if p[0]<=target]
 if not candidates:return None
 return round(pts[-1][1]-candidates[-1][1],3)

class TextExtractor(HTMLParser):
 def __init__(self):
  super().__init__(); self.parts=[]
 def handle_data(self,data):
  if data.strip(): self.parts.append(data.strip())
 def text(self): return ' '.join(self.parts)

def fetch_text(url):
 req=Request(url,headers={'User-Agent':'Mozilla/5.0 HidroRS/4.0 (monitoramento informativo)','Accept':'text/html'})
 with urlopen(req,timeout=18,context=TLS) as res:
  raw=res.read(2_000_001)
  if len(raw)>2_000_000:raise ValueError('Página muito grande')
  parser=TextExtractor();parser.feed(raw.decode('utf-8','replace'));return parser.text()

def number(v):return float(v.replace('.','').replace(',','.'))

def parse_aggregator_page(text):
 # Somente valores explicitamente acompanhados de unidade e data.
 m=re.search(r'([0-9]{1,2}[,.][0-9]{1,2})\s*m(?:\s+metros)?\s+([0-9]{1,3}%\s+)?Atualizado em\s+(\d{2}/\d{2}/\d{4})\s+às\s+(\d{2}:\d{2})',text,re.I)
 if not m:raise ValueError('Formato de leitura da página não reconhecido')
 level=number(m.group(1));stamp=datetime.strptime(m.group(3)+' '+m.group(4),'%d/%m/%Y %H:%M').replace(tzinfo=TZ)
 cot=re.search(r'Cota de inundação\s+([0-9]{1,2}[,.][0-9]{1,2})\s*m',text,re.I)
 return level,stamp,number(cot.group(1)) if cot else None

def parse_agorars(text, river, city):
 # Cada medição pertence ao bloco entre o título da estação e o título seguinte.
 # Evita capturar o índice, gráficos e números de estações vizinhas.
 aliases={'Manuel Viana':'Manoel Viana'}
 cityname=aliases.get(city,city)
 station_title=r'Rio\s+'+re.escape(river)+r'\s*[-–—]\s*'+re.escape(cityname)+r'(?=\s|$)'
 starts=list(re.finditer(station_title,text,re.I))
 if not starts:raise ValueError('Estação não localizada no Agora RS: '+river+' / '+cityname)
 for m in starts:
  # A página pode repetir nomes em menus: só aceitar bloco com medição próxima.
  rest=text[m.end():]
  dt=re.search(r'Medição de\s+(\d{2}/\d{2}/\d{4})\s+às\s+(\d{2})\s*h\s*(\d{2})',rest[:450],re.I)
  if not dt:continue
  # Corta na primeira fonte ou no próximo cabeçalho de estação.
  end=re.search(r'Fonte dos dados\s*:',rest[dt.end():],re.I)
  if not end:continue
  block=rest[:dt.end()+end.start()]
  # Primeiro nível explicitamente associado à situação; não confundir com gráfico.
  lev=re.search(r'Situação\s*:\s*[^0-9]{0,100}?\s*(-?\d{1,2}[,.]\d{2})\s+metros\b',block[dt.end():dt.end()+300],re.I)
  if not lev:continue
  cot=re.search(r'Cota de inundação\s*:\s*(\d{1,2}[,.]\d{2})\s*metros',block,re.I)
  stamp=datetime.strptime(dt.group(1)+' '+dt.group(2)+':'+dt.group(3),'%d/%m/%Y %H:%M').replace(tzinfo=TZ)
  changes={}
  for h in (1,6,24):
   trend=re.search(r'(?<!\d)'+str(h)+r'\s*h\s*([↑↓])\s*(\d+)\s*cm',block)
   changes[h]=round(int(trend.group(2))/100*(1 if trend.group(1)=='↑' else -1),3) if trend else None
  return number(lev.group(1)),stamp,number(cot.group(1)) if cot else None,changes
 raise ValueError('Medição ou cota não reconhecida para '+river+' / '+cityname)

def fetch_station(name,conf,official_items=None,official_error=None):
 now=time.time()
 with LOCK:cached=CACHE.get(name)
 if cached and now-cached[0]<TTL:return cached[1]
 base={'id':conf['code'],'name':name,'river':conf['river'],'city':conf['city'],'lat':float(conf['lat']),'lon':float(conf['lon'])}
 failures=[];parsed=None;source=None;link=None;changes={}
 # Prioridade: estação oficial com correspondência única e leitura válida.
 if official_items is not None:
  candidates=[i for i in official_items if defesa_civil.matches(i,conf['river'],conf['city'])]
  station_code=conf.get('official_code')
  if station_code:
   candidates=[i for i in official_items if defesa_civil.same_station_code(i.get('codigo'),station_code)]
  if len(candidates)==1:
   item=candidates[0];rio=((item.get('data') or {}).get('rio') or {})
   level=defesa_civil.numeric(defesa_civil.val(rio.get('rio_nivel')))
   if level is not None:
    stamp=parse_time(item.get('timestamp'))
    age=(datetime.now(TZ)-stamp).total_seconds()/3600 if stamp else 999
    stale=age>3 or age<-.5
    delta=defesa_civil.numeric(defesa_civil.val(rio.get('rio_nivel_tendencia')))
    result={**base,'level':level,'time':stamp.isoformat() if stamp else None,'age_hours':round(age,2),
      'stale':stale,'status':'desatualizado' if stale else 'sem_cota','flood_level_m':None,
      'd1':delta,'d6':None,'d24':None,'source':'Defesa Civil RS — rede hidrometeorológica',
      'url':conf.get('official_url') or 'https://redehidrometeorologica.defesacivil.rs.gov.br/',
      'data_kind':'oficial','official_code':item.get('codigo'),'history':[],
      'flood_cota_origin':None,
      'error':'Cota de inundação compatível não verificada; sem classificação percentual de risco.'}
    with LOCK:CACHE[name]=(now,result)
    return result
  elif len(candidates)>1:
   failures.append('Mais de uma estação oficial possível; confira /api/candidates antes de selecionar o código')
 if official_items is not None and conf.get('official_code') and not candidates:failures.append('Código oficial '+conf['official_code']+' não localizado na resposta da API')
 if official_error:failures.append('API oficial: '+official_error)

 if conf.get('url'):
  try:
   parsed=parse_aggregator_page(fetch_text(conf['url']));source=conf['source'];link=conf['url']
  except Exception as exc:failures.append('Agregador: '+str(exc))
 if parsed is None:
  try:
   level,stamp,cota,changes=parse_agorars(fetch_text(conf.get('agorars_url') or 'https://agorars.com/monitor-dos-rios/'),conf['river'],conf['city'])
   parsed=(level,stamp,cota);source='Agora RS (agregador de dados ANA/RHN)';link=conf.get('agorars_url') or 'https://agorars.com/monitor-dos-rios/'
  except Exception as exc:failures.append('Agora RS: '+str(exc))
 if parsed:
  level,stamp,cota=parsed;age=(datetime.now(TZ)-stamp).total_seconds()/3600;stale=age>3 or age<-.5
  # Regra preventiva própria do painel, NÃO é cota oficial de atenção.
  # Aplicar somente a cotas positivas e compatíveis com a mesma régua/referência.
  status='sem_cota' if cota is None or cota<=0 else 'inundacao' if level>=cota else 'proximo' if level>=0.90*cota else 'normal'
  if stale:status='desatualizado'
  result={**base,'level':level,'time':stamp.isoformat(),'age_hours':round(age,2),'stale':stale,'status':status,'flood_level_m':cota,
   'd1':changes.get(1),'d6':changes.get(6),'d24':changes.get(24),'source':source,'url':link,'data_kind':'real_agregador','history':[],'flood_cota_origin':'fonte consultada' if cota is not None else None}
 else:
  result={**base,'level':None,'status':'indisponivel','stale':True,'error':'; '.join(failures),'source':conf['source'],'url':conf.get('url'),'data_kind':'sem_dados','history':[],'flood_level_m':None,'flood_cota_origin':None,'d1':None,'d6':None,'d24':None}
 with LOCK:CACHE[name]=(now,result)
 return result

class Handler(SimpleHTTPRequestHandler):
 def send_json(self,body,code=200):
  b=json.dumps(body,ensure_ascii=False).encode('utf-8')
  self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_GET(self):
  path=urlparse(self.path).path
  if path=='/api/health':return self.send_json({'status':'online','tls':TLS_NAME,'sources':len(SOURCES),'note':'Saúde do servidor não comprova disponibilidade dos feeds externos'})
  if path=='/api/stations':
   official,official_error=defesa_civil.read()
   with ThreadPoolExecutor(max_workers=8) as pool:
    items=list(pool.map(lambda pair:fetch_station(pair[0],pair[1],official,official_error),SOURCES.items()))
   return self.send_json({'stations':items,'updated_at':datetime.now(TZ).isoformat(),'coverage':'12 municípios prioritários; extração das páginas públicas, sem cobertura integral da rede do RS','simulation':False})
  if path=='/api/candidates':
   official,err=defesa_civil.read()
   return self.send_json({'official_api_error':err,'Santa Maria':defesa_civil.candidate_summary(official or [],'Arroio Cadena','Santa Maria'),'Arroio do Meio':defesa_civil.candidate_summary(official or [],'Forqueta','Arroio do Meio'),'codigos_configurados':['DCRS-00027','DCRS-00104'],'instruction':'Uma correspondência única permite mostrar nível; mais de uma exige validação do código oficial. A API não fornece cota numérica.'})
  if path=='/api/history':
   code=parse_qs(urlparse(self.path).query).get('station',[''])[0]
   station=next((fetch_station(n,c) for n,c in SOURCES.items() if c['code']==code),None)
   return self.send_json(station or {'error':'Estação não encontrada'},200 if station else 404)
  if path=='/':self.path='/index.html'
  return super().do_GET()

if __name__=='__main__':
 os.chdir(BASE)
 port=int(os.environ.get('PORT','8000'))
 host='0.0.0.0' if os.environ.get('PORT') else '127.0.0.1'
 print(f'Dashboard Hidrológico RS: http://{host}:{port}',flush=True)
 print('TLS:',TLS_NAME,'| Fontes configuradas:',len(SOURCES),flush=True)
 ThreadingHTTPServer((host,port),Handler).serve_forever()
