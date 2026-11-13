import ssl, csv, json, os, threading, time, urllib.request, urllib.error, urllib.parse, unicodedata, webbrowser, xml.etree.ElementTree as ET, re
from html.parser import HTMLParser
from html import unescape
from urllib.parse import urljoin, urlparse
from datetime import timedelta, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parent
BASE="https://clima.rs.gov.br/climars/api/v1/ana/cotas/municipio/"
IBGE="https://servicodados.ibge.gov.br/api/v1/localidades/estados/43/municipios"
MUNICIPIOS=["São Borja","Itaqui","Uruguaiana","Manoel Viana","Alegrete","Quaraí","Rosário do Sul","Rio Pardo","Dona Francisca","Estrela","São Sepé","Arroio do Meio","Lajeado","Fontoura Xavier","Pouso Novo","São Gabriel","Cachoeira do Sul","São Jerônimo","Santiago","Bossoroca","Garruchos","Santo Antônio das Missões","São Luiz Gonzaga","São Nicolau","São Francisco de Assis","Candelária","Pantano Grande","Encruzilhada do Sul","Santa Cruz do Sul","General Câmara","Restinga Sêca","Paraíso do Sul","Formigueiro","Vila Nova do Sul","Caçapava do Sul","São Gabriel","Travesseiro","Marques de Souza"]
# Códigos oficiais previamente conhecidos; permitem continuar se o IBGE estiver indisponível.
CODIGOS_FIXOS={"sao borja":"4318002","itaqui":"4310603","uruguaiana":"4322400","alegrete":"4300406","quarai":"4315305","rio pardo":"4315701","estrela":"4307807","lajeado":"4311403","cachoeira do sul":"4303004","rosario do sul":"4316402","sao sepe":"4319604","manoel viana":"4311759","quarai":"4315305","pouso novo":"4315131"}
CACHE={"time":0,"data":None}
LOCK=threading.Lock()
LOCK_SAO_BORJA=threading.Lock()
CACHE_SAO_BORJA={"time":0,"data":None}

def normal(s):
    return "".join(c for c in unicodedata.normalize("NFD",str(s or "").lower()) if unicodedata.category(c)!="Mn").strip()

def getjson(url,timeout=14):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 Dashboard-Hidrologico-3DE/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8-sig"))


SACE_URL="https://sace.sgb.gov.br/niveis/estacoes_lista.php?bacia=uruguai"
SACE_BACIA="https://sace.sgb.gov.br/index_bacias_monitoradas.php?getbacia=buruguai"

class SaceTable(HTMLParser):
    def __init__(self):
        super().__init__();self.rows=[];self.row=None;self.cell=None;self.depth=0
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]
        elif tag=='br' and self.cell is not None:self.cell.append(' ')
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
        elif tag=='tr' and self.row is not None:
            if self.row:self.rows.append(self.row)
            self.row=None

# ID 33 é a estação Passo São Borja no export CSV fornecido pelo usuário.
# A URL de exportação pode mudar no SACE. A descoberta abaixo é conservadora:
# somente aceita um CSV cuja identidade da estação esteja confirmada.
SACE_STATION_ID="33"
SACE_EXPORT_URL="https://sace.sgb.gov.br/niveis/api/dados/uruguai_33_cota.csv"

SACE_PAGES=[
 "https://sace.sgb.gov.br/niveis/estacoes_lista.php?bacia=uruguai",
 "https://sace.sgb.gov.br/index_bacias_monitoradas.php?getbacia=buruguai",
]

def contexto_ssl_sace():
    """TLS validado pelo repositório de certificados do sistema (truststore).
    Nunca desabilita a verificação de certificado ou hostname.
    """
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()

def fetch_sace(url, accept='text/html,text/csv,*/*', timeout=12):
    req=urllib.request.Request(url,headers={
      'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
      'Accept':accept,'Referer':SACE_BACIA,
      'Cache-Control':'no-cache', 'Accept-Encoding':'identity', 'Connection':'close'})
    with urllib.request.urlopen(req,timeout=timeout,context=contexto_ssl_sace()) as r:
        final=r.geturl()
        if urlparse(final).hostname not in ('sace.sgb.gov.br',):
            raise ValueError('Redirecionamento para domínio não autorizado')
        raw=r.read(3_000_001)
        if len(raw)>3_000_000:raise ValueError('Resposta SACE excede 3 MB')
        return raw,final,r.headers.get('Content-Type','')

def parse_sace_csv(raw):
    for enc in ('utf-8-sig','latin-1'):
        try:conteudo=raw.decode(enc);break
        except UnicodeDecodeError:continue
    if 'data_hora_medicao' not in conteudo[:500].lower() or 'indice' not in conteudo[:500].lower():
        raise ValueError('Resposta não contém cabeçalho CSV esperado')
    linhas=[]
    for row in csv.DictReader(conteudo.splitlines(),delimiter=';'):
        try:
            dt=datetime.strptime(row['data_hora_medicao'].strip(),'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=-3)))
            cm=float(row['indice'].strip().replace(',','.'))
            if 0<=cm<=3000:linhas.append((dt,cm))
        except (KeyError,ValueError,TypeError):continue
    if not linhas:raise ValueError('CSV sem leituras válidas')
    linhas.sort(key=lambda t:t[0]);return linhas

def montar_estacao_sace(registros,fonte, *, codigo="75780000", nome="Passo São Borja", municipio="São Borja", rio="RIO URUGUAI", inundacao_cm=900, latitude=-28.66, longitude=-56.00):
    instante,cm=registros[-1]
    horas=(datetime.now(timezone(timedelta(hours=-3)))-instante).total_seconds()/3600
    if horas < -1:raise ValueError('Data da medição no futuro')
    antigo=horas>=12
    delta=None;tendencia=None
    anteriores=[(abs((instante-d).total_seconds()-3600),v) for d,v in registros[:-1] if 1800<=(instante-d).total_seconds()<=5400]
    if anteriores:
        _,prev=min(anteriores)
        delta=round(cm-prev,1)
        tendencia=('Subiu ' if delta>0 else 'Desceu ' if delta<0 else 'Estável')+(f'{abs(delta):g} cm na última hora' if delta else ' na última hora')
    else:
        # Quando não há medição de 1 hora atrás, calcula a variação em 24 horas.
        # Prioriza um registro no instante exato; admite aproximação de até 2h,
        # identificando explicitamente que o período é aproximado.
        alvo=instante-timedelta(hours=24)
        candidatos=[(abs((d-alvo).total_seconds()),d,v) for d,v in registros[:-1]
                    if abs((d-alvo).total_seconds())<=7200]
        if candidatos:
            desvio,d_anterior,prev=min(candidatos,key=lambda x:x[0])
            delta=round(cm-prev,1)
            periodo='nas últimas 24 horas' if desvio<=60 else 'em aproximadamente 24 horas'
            if delta>0:
                tendencia=f'Subiu {abs(delta)/100:.2f} m {periodo}'.replace('.',',')
            elif delta<0:
                tendencia=f'Desceu {abs(delta)/100:.2f} m {periodo}'.replace('.',',')
            else:
                tendencia=f'Estável {periodo}'
    return {'codigoEstacao':codigo,'nomeEstacao':nome,'municipio':municipio,
      'rio':rio,'nivelCm':cm,'nivelMetros':cm/100,
      'dataHoraMedicao':instante.strftime('%d/%m/%Y %H:%M:%S'),
      'dataHoraShort':instante.strftime('%d/%m %H:%M'),
      'confiavel':not antigo,'status':'INUNDACAO' if cm>=inundacao_cm else ('DADO_ANTIGO' if antigo else 'NORMAL'),
      'statusLabel':('Inundação (dado antigo)' if antigo else 'Inundação') if cm>=inundacao_cm else ('Dado antigo (SACE)' if antigo else 'Abaixo da inundação'),
      'taxaMudanca':tendencia,'taxaMudancaValor':delta,'fonte':fonte,
      'horasDesdeMedicao':round(horas,1),'dadoAntigo':antigo,
      'latitude':latitude,'longitude':longitude}

def descobrir_csv_sace(html,base):
    """Extrai links explícitos para CSV da estação 33; não adivinha URLs do portal."""
    candidatos=[]
    # Link href e botões que apontem explicitamente ao export CSV da estação 33.
    for m in re.finditer(r'(?:href|data-url)\s*=\s*[\x22\x27]([^\x22\x27<>]+)[\x22\x27]',html,re.I):
        link=unescape(m.group(1)).replace('\\/','/')
        contexto=html[max(0,m.start()-160):m.end()+160].lower()
        if not (('csv' in link.lower() or 'export' in link.lower() or 'download' in link.lower()) and
                (re.search(r'(?<!\d)33(?!\d)',link) or 'passo s' in contexto and 'borja' in contexto)):
            continue
        url=urljoin(base,link)
        if urlparse(url).hostname=='sace.sgb.gov.br' and url not in candidatos:candidatos.append(url)
    return candidatos[:4]

def consultar_sace_sao_borja():
    diag={'fonte':'SACE/SGB','estacao':'Passo São Borja','codigoANA':'75780000',
          'idExportacaoCSV':SACE_STATION_ID,'modo':'consulta HTTP automática a cada 5 minutos','tentativas':[], 'urlConfigurada':SACE_EXPORT_URL}
    urls=[]
    if SACE_EXPORT_URL:
        if urlparse(SACE_EXPORT_URL).scheme=='https' and urlparse(SACE_EXPORT_URL).hostname=='sace.sgb.gov.br':
            urls.append(SACE_EXPORT_URL)
        else:diag['configuracao']='URL configurada inválida (somente HTTPS no SACE)'
    # Endereço confirmado pelo usuário no Firefox (Rede > Cabeçalhos, HTTP 200).
    # A consulta é direta: não depende da descoberta de links em páginas HTML.
    for url in urls[:5]:
        try:
            raw,final,content_type=fetch_sace(url,'text/csv,*/*')
            registros=parse_sace_csv(raw)
            est=montar_estacao_sace(registros,'SACE/SGB (automático)')
            diag.update({'urlCSV':final,'registrosValidos':len(registros),'tipoResposta':content_type,
                'ultimaMedicao':est['dataHoraMedicao'],'nivelCm':est['nivelCm'],
                'horasDesdeMedicao':est['horasDesdeMedicao'],'automatico':True})
            return est,diag
        except Exception as e:diag['tentativas'].append({'csv':url,'erro':str(e)[:200]})
    diag['automatico']=False
    diag['observacao']='Consulta automática ao CSV do SACE falhou. Veja tentativas e erro; CSV local é apenas histórico, não atualização em tempo real.'
    return None,diag


# URL de exportação fornecida pelo usuário a partir do SACE/SGB (Rio Pardo, bacia Guaíba).
RIO_PARDO_SACE_CSV="https://sace.sgb.gov.br/niveis/api/dados/guaiba_55_cota.csv"
def consultar_sace_rio_pardo():
    diag={'fonte':'SACE/SGB','estacao':'Rio Pardo','codigoANA':'85900000',
          'idExportacaoCSV':'guaiba_55','modo':'consulta HTTP automática a cada 5 minutos',
          'urlConfigurada':RIO_PARDO_SACE_CSV,'automatico':False}
    try:
        raw,url,content_type=fetch_sace(RIO_PARDO_SACE_CSV,'text/csv,*/*')
        registros=parse_sace_csv(raw)
        est=montar_estacao_sace(registros,'SACE/SGB (automático)',
             codigo='85900000',nome='Rio Pardo',municipio='Rio Pardo',
             rio='RIO JACUÍ',inundacao_cm=1250,latitude=-29.99,longitude=-52.38)
        diag.update({'automatico':True,'urlCSV':url,'registrosValidos':len(registros),
                     'ultimaMedicao':est['dataHoraMedicao'],'nivelCm':est['nivelCm'],
                     'horasDesdeMedicao':est['horasDesdeMedicao'],
                     'dadoAntigo':est['dadoAntigo']})
        return est,diag
    except Exception as exc:
        diag['erro']=str(exc)[:250]
        diag['observacao']='Não foi possível consultar o SACE; não usar valores inventados ou de outras estações.'
        return None,diag

# Histórico local da estação de referência Estrela/Lajeado.
# Calcula a variação real em aproximadamente uma hora, sem assumir que
# taxaMudancaValor (que pode representar 24h) é uma taxa horária.
HIST_LAJEADO=ROOT/"historico_lajeado.json"
def atualizar_tendencia_lajeado(estacoes):
    candidatos=[e for e in estacoes if ("estrela" in normal(e.get("nomeEstacao"))
               or normal(e.get("municipio"))=="estrela")
               and "taquari" in normal(e.get("rio")) and e.get("nivelMetros") is not None]
    if not candidatos:return
    est=max(candidatos,key=lambda e:str(e.get("dataHoraMedicao") or ""))
    codigo=str(est.get("codigoEstacao") or est.get("nomeEstacao"))
    medicao=str(est.get("dataHoraMedicao") or "")
    try:nivel=float(est["nivelMetros"])
    except (TypeError,ValueError):return
    if not medicao or not (0 <= nivel <= 50):return
    try:
        historico=json.loads(HIST_LAJEADO.read_text(encoding="utf-8"))
        if not isinstance(historico,list):historico=[]
    except (OSError,ValueError):historico=[]
    agora=time.time()
    historico=[x for x in historico if x.get("codigo")==codigo
              and 0<=agora-float(x.get("coletado",0))<=172800]
    if not any(x.get("medicao")==medicao for x in historico):
        historico.append({"codigo":codigo,"medicao":medicao,"nivel":nivel,"coletado":agora})
        try:HIST_LAJEADO.write_text(json.dumps(historico,ensure_ascii=False),encoding="utf-8")
        except OSError:pass
    # Apenas medições distintas da mesma estação, coletadas 45-75 min antes.
    anteriores=[x for x in historico if x["medicao"]!=medicao
                and 2700<=agora-x["coletado"]<=4500]
    if not anteriores:return
    ref=min(anteriores,key=lambda x:abs(agora-x["coletado"]-3600))
    delta=round((nivel-float(ref["nivel"]))*100,1)
    if abs(delta)<0.05:descricao="Estável na última hora"
    else:
        verbo="Desceu" if delta<0 else "Subiu"
        valor=abs(delta)
        num=("%g"%valor).replace(".",",")
        descricao=f"{verbo} {num} cm na última hora"
    est["taxaMudanca"]=descricao
    est["taxaMudancaValor"]=delta
    est["tendenciaPeriodo"]="aproximadamente 1 hora"
    est["tendenciaFonte"]="Histórico local das medições ClimaRS da estação Estrela"

def consultar():
    dados={"consultadoEm":datetime.now().astimezone().isoformat(),"fonte":"ClimaRS / ANA","estacoes":[],"erros":[],"municipiosConsultados":[]}
    try:
        municipios=getjson(IBGE)
        codigos={**CODIGOS_FIXOS, **{normal(m["nome"]):str(m["id"]) for m in municipios}}
    except Exception as e:
        dados["erros"].append("Não foi possível obter códigos IBGE: "+str(e))
        codigos=CODIGOS_FIXOS.copy()
    vistos=set()
    for nome in MUNICIPIOS:
        codigo=codigos.get(normal(nome))
        if not codigo:
            dados["erros"].append("Município não localizado no IBGE: "+nome)
            continue
        try:
            resultado=getjson(BASE+codigo)
            if not isinstance(resultado,list):
                raise ValueError("Resposta não é lista de estações")
            dados["municipiosConsultados"].append(nome)
            for item in resultado:
                if not isinstance(item,dict):continue
                chave=str(item.get("codigoEstacao") or "") or (str(item.get("nomeEstacao"))+str(item.get("municipio")))
                if chave not in vistos:
                    vistos.add(chave)
                    dados["estacoes"].append(item)
        except Exception as e:
            dados["erros"].append(nome+": "+str(e))
    atualizar_tendencia_lajeado(dados['estacoes'])
    dados['estacoes']=[e for e in dados['estacoes'] if not (normal(e.get('municipio'))=='sao borja' and str(e.get('codigoEstacao'))!='75780000')]
    # Fonte EXCLUSIVA de São Borja: SACE/SGB, estação Passo São Borja (ANA 75780000).
    # Não usar ClimaRS, ANA legado, Garruchos ou arquivo CSV local como substitutos.
    dados['estacoes']=[e for e in dados['estacoes'] if str(e.get('codigoEstacao'))!='75780000']
    try:
        with LOCK_SAO_BORJA:
            if CACHE_SAO_BORJA['data'] is None or time.time()-CACHE_SAO_BORJA['time'] >= 300:
                est,diag=consultar_sace_sao_borja()
                CACHE_SAO_BORJA.update(time=time.time(),data={'estacao':est,'diagnostico':diag})
            else:
                est=CACHE_SAO_BORJA['data']['estacao'];diag=CACHE_SAO_BORJA['data']['diagnostico']
        dados['saceDiagnostico']=diag
        if est:
            dados['estacoes'].append(est)
        else:
            dados['erros'].append('São Borja: SACE indisponível; nenhuma fonte alternativa será usada.')
    except Exception as e:
        dados['saceDiagnostico']={'automatico':False,'fonte':'SACE/SGB',
                                  'estacao':'Passo São Borja','erro':str(e)}
        dados['erros'].append('São Borja: falha na consulta SACE; sem leitura.')
    # Rio Pardo: prioridade ao CSV direto do SACE/SGB; nenhuma estação vizinha substitui a 85900000.
    est_rp,diag_rp=consultar_sace_rio_pardo()
    dados['saceRioPardoDiagnostico']=diag_rp
    if est_rp:
        dados['estacoes']=[e for e in dados['estacoes'] if str(e.get('codigoEstacao'))!='85900000']
        dados['estacoes'].append(est_rp)
    # ANA legado: consultar por código, apenas quando o ClimaRS não devolve a estação.
    # Não confundir dado antigo com nível atual.
    for nome,codigo in {"Rio Pardo":"85900000"}.items():
        if any(str(x.get("codigoEstacao"))==codigo for x in dados["estacoes"]):continue
        try:
            est,diag=ana_por_codigo(nome,codigo)
            dados.setdefault("anaDiagnostico",[]).append(diag)
            if est:dados["estacoes"].append(est)
        except Exception as e:
            dados["erros"].append("ANA legado "+nome+": "+str(e))
    return dados

def ana_por_codigo(nome,codigo):
    agora=datetime.now().astimezone()
    ini=(agora.date()-timedelta(days=14)).strftime("%d/%m/%Y")
    fim=agora.date().strftime("%d/%m/%Y")
    q=urllib.parse.urlencode({"CodEstacao":codigo,"DataInicio":ini,"DataFim":fim})
    url="https://www.ana.gov.br/telemetria1ws/ServiceANA.asmx/DadosHidrometeorologicos?"+q
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"text/xml,application/xml"})
    with urllib.request.urlopen(req,timeout=18) as r:raw=r.read()
    root=ET.fromstring(raw)
    # Procura registros com DataHora e Nivel no mesmo elemento (evita misturar linhas).
    registros=[]
    for el in root.iter():
        filhos={x.tag.split("}")[-1].lower():(x.text or "").strip() for x in el}
        if "datahora" not in filhos or "nivel" not in filhos:continue
        if filhos.get("codestacao",codigo)!=codigo:continue
        try:
            instante=datetime.fromisoformat(filhos["datahora"].strip().replace(" ","T",1))
            nivel=float(filhos["nivel"].replace(",","."))
            if instante.tzinfo is None:instante=instante.replace(tzinfo=timezone(timedelta(hours=-3)))
            if 0<=nivel<=100000:registros.append((instante,nivel))
        except (ValueError,TypeError):continue
    registros.sort(key=lambda x:x[0],reverse=True)
    diag={"municipio":nome,"codigo":codigo,"registros":len(registros),"fonte":"ANA telemetria legado"}
    if not registros:return None,diag
    instante,nivel=registros[0]
    horas=(agora-instante).total_seconds()/3600
    diag.update({"ultimaMedicao":instante.isoformat(),"horasDesdeMedicao":round(horas,1),"nivelBrutoCm":nivel})
    # Segurança: não publicar valores antigos como leitura operacional atual.
    if horas<0 or horas>24:
        diag["observacao"]="Dado antigo ou horário inconsistente; não publicado como atual"
        return None,diag
    # Nível ANA é expresso em centímetros na API de telemetria.
    return {"codigoEstacao":codigo,"nomeEstacao":nome,"municipio":nome,
      "rio":{"São Borja":"RIO URUGUAI","Rio Pardo":"RIO JACUÍ","São Sepé":"RIO SÃO SEPÉ"}[nome],
      "nivelCm":nivel,"nivelMetros":nivel/100,"dataHoraMedicao":instante.strftime("%d/%m/%Y %H:%M:%S"),
      "dataHoraShort":instante.strftime("%d/%m %H:%M"),"confiavel":False,"status":"SEM_CLASSIFICACAO",
      "statusLabel":"Sem classificação (ANA)","taxaMudanca":None,"taxaMudancaValor":None,
      "fonte":"ANA legado","horasDesdeMedicao":round(horas,1)},diag


# Previsão pluviométrica: modelo ECMWF IFS (serviço Open-Meteo).
# Média espacial do acumulado de sete dias em seis municípios de referência
# distribuídos pela área monitorada, NÃO uma previsão oficial da 3ª DE.
PONTOS_PREVISAO=[
    ("São Borja",-28.660,-56.004),
    ("Uruguaiana",-29.760,-57.087),
    ("Santa Maria",-29.686,-53.807),
    ("Cachoeira do Sul",-30.039,-52.894),
    ("Rio Pardo",-29.990,-52.378),
    ("Lajeado",-29.467,-51.961),
]
PREVISAO_CACHE={"time":0,"data":None}
PREVISAO_LOCK=threading.Lock()
def consultar_previsao():
    # Todos os dias são calculados na timezone local para evitar datas UTC.
    hoje=datetime.now(timezone(timedelta(hours=-3))).date()
    datas=[(hoje+timedelta(days=i)).isoformat() for i in range(7)]
    pontos=[]
    erros=[]
    for nome,lat,lon in PONTOS_PREVISAO:
        params=urllib.parse.urlencode({
            "latitude":lat,"longitude":lon,"daily":"precipitation_sum",
            "models":"ecmwf_ifs","timezone":"America/Sao_Paulo",
            "forecast_days":7,"precipitation_unit":"mm"
        })
        url="https://api.open-meteo.com/v1/forecast?"+params
        try:
            j=getjson(url,timeout=12)
            diario=j.get("daily") or {}
            dias=diario.get("time") or []
            valores=diario.get("precipitation_sum") or []
            mapa={d:v for d,v in zip(dias,valores)}
            if not all(d in mapa and isinstance(mapa[d],(int,float))
                       and 0<=mapa[d]<=1000 for d in datas):
                raise ValueError("Previsão incompleta ou datas incorretas")
            pontos.append({"municipio":nome,"acumulado7dMm":round(sum(mapa[d] for d in datas),1)})
        except Exception as exc:
            erros.append({"municipio":nome,"erro":str(exc)})
    # Não apresentar uma média enganosa com cobertura incompleta.
    if len(pontos)!=len(PONTOS_PREVISAO):
        return {"disponivel":False,"erro":"Cobertura incompleta da previsão; sem classificação automática",
                "pontosRecebidos":len(pontos),"pontosEsperados":len(PONTOS_PREVISAO),
                "erros":erros,"inicio":datas[0],"fim":datas[-1],
                "fonte":"ECMWF IFS via Open-Meteo"}
    media=round(sum(p["acumulado7dMm"] for p in pontos)/len(pontos),1)
    return {"disponivel":True,"mediaMm":media,"acima50":media>50,
            "inicio":datas[0],"fim":datas[-1],"pontos":pontos,
            "fonte":"Modelo ECMWF IFS via Open-Meteo",
            "metodologia":"Média de seis acumulados municipais de 7 dias (não é probabilidade operacional)",
            "atualizadoEm":datetime.now(timezone(timedelta(hours=-3))).isoformat()}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(ROOT),**kwargs)
    def do_GET(self):
        if self.path.split("?")[0] == "/api/previsao":
            with PREVISAO_LOCK:
                # 3 horas de cache, mas falhas são reconsultadas em 15 minutos.
                anterior=PREVISAO_CACHE["data"]
                ttl=10800 if anterior and anterior.get("disponivel") else 900
                if anterior is None or anterior.get("inicio")!=datetime.now(timezone(timedelta(hours=-3))).date().isoformat() or time.time()-PREVISAO_CACHE["time"]>=ttl:
                    PREVISAO_CACHE.update(data=consultar_previsao(),time=time.time())
                data=PREVISAO_CACHE["data"]
            body=json.dumps(data,ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] == "/api/sao-borja":
            # Consulta independente: não aguarda as dezenas de requisições ClimaRS.
            with LOCK_SAO_BORJA:
                if CACHE_SAO_BORJA['data'] is None or time.time()-CACHE_SAO_BORJA['time'] >= 300:
                    try:
                        est,diag=consultar_sace_sao_borja()
                    except Exception as exc:
                        est=None;diag={'automatico':False,'erro':repr(exc),'urlConfigurada':SACE_EXPORT_URL}
                    CACHE_SAO_BORJA.update(time=time.time(),data={'estacao':est,'diagnostico':diag})
                data=CACHE_SAO_BORJA['data']
            body=json.dumps(data,ensure_ascii=False).encode('utf-8')
            self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
            return
        if self.path.split("?")[0] in ("/api/stations","/api/diagnostico"):
            with LOCK:
                if CACHE["data"] is None or time.time()-CACHE["time"]>300:
                    novo=consultar()
                    CACHE["data"]=novo
                    CACHE["time"]=time.time()
                data=CACHE["data"]
            body=json.dumps(data,ensure_ascii=False).encode("utf-8")
            self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)))
            self.end_headers();self.wfile.write(body)
        else:super().do_GET()

if __name__=="__main__":
    os.chdir(ROOT)
    print("Dashboard Hidrológico da 3ª DE")
    print("Abra no navegador: http://localhost:8000")
    print("As consultas oficiais podem demorar na primeira atualização.")
    print("Mantenha esta janela aberta. Para encerrar: Ctrl+C.")
    try:
        server=ThreadingHTTPServer(("127.0.0.1",8000),Handler)
    except OSError as e:
        print("ERRO ao iniciar na porta 8000:",e)
        input("Pressione Enter para fechar...")
        raise SystemExit(1)
    threading.Timer(1.2,lambda:webbrowser.open("http://localhost:8000")).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
