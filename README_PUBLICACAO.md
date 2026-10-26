# Dashboard Hidrológico RS — publicação no Render

Este pacote foi preparado a partir da versão V8.6 com os pontos de Rosário do Sul e Santa Maria / Arroio Cadena. Não substitui alertas oficiais. As consultas externas podem falhar ou ficar desatualizadas.

## Publicar pela primeira vez

1. Acesse https://github.com e crie sua conta (se necessário).
2. Clique em **New repository**, dê o nome `dashboard-hidrologico-rs` e escolha **Private** (ou Public, se desejar disponibilizar o código).
3. Dentro do repositório, clique em **Add file > Upload files**. Envie os arquivos desta pasta **sem incluir a pasta externa** (index.html, app.js, style.css, server.py, defesa_civil.py, stations.json, requirements.txt, render.yaml, .gitignore e este README). Clique em **Commit changes**. Arquivos começando com ponto podem não aparecer no Windows; `.gitignore` é opcional.
4. Acesse https://dashboard.render.com e entre com sua conta. Clique em **New > Web Service**.
5. Conecte sua conta GitHub e selecione o repositório `dashboard-hidrologico-rs`.
6. Escolha **Language: Python 3**, **Instance Type: Free**, **Build Command: `pip install -r requirements.txt`**, **Start Command: `python server.py`**. Deixe **Root Directory** em branco.
7. Clique em **Deploy Web Service** e aguarde o status **Live**.
8. Copie a URL HTTPS terminada em `.onrender.com` exibida no painel e envie para os colegas.

**A URL não existe antes da publicação.** A hospedagem Free pode adormecer após 15 minutos sem acessos e levar cerca de 1 minuto para iniciar novamente. Para uso operacional contínuo, escolha hospedagem com disponibilidade adequada e valide as fontes.

## Verificação

- `https://SEU-ENDERECO.onrender.com/api/health`: servidor online (não garante leituras).
- `https://SEU-ENDERECO.onrender.com/api/stations`: medições, horários, erros de fonte e estações sem dados.
- `https://SEU-ENDERECO.onrender.com/api/candidates`: diagnóstico da Defesa Civil.

## Teste local no Windows

Abra CMD nesta pasta, execute `python server.py` e acesse http://127.0.0.1:8000.

## Atenção

- Os arquivos **não contêm senha**. O repositório pode ser privado, mas o endereço do Web Service será público.
- O Render deve conseguir acessar os serviços externos via HTTPS. Se a API oficial tiver certificado inválido ou estiver fora do ar, o dashboard pode mostrar `Sem dados`; o código não ignora erros de certificado.
- O mapa usa coordenadas de referência e não substitui as posições verificadas dos sensores.
- Nunca divulgue leituras sem conferir horário, estação e referência da cota.
