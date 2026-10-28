"""Gera o dossiê offline, seu inventário de rotas e a edição PDF. Sem acesso à aplicação."""
from pathlib import Path
import ast
import html
import json
import math
import subprocess

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
SRC = ROOT / 'src' / 'secretaria'
E = html.escape
SHA = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()

def literal(node, default=''):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return default

def inventory():
    # Somente módulos registrados em main.py, sem importar Settings nem ler .env.
    tree = ast.parse((SRC / 'main.py').read_text(encoding='utf-8'))
    modules = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('secretaria.api'):
            for alias in node.names:
                modules[alias.asname or alias.name] = node.module + '.' + alias.name
    registered = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'include_router':
            arg = node.args[0]
            if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                registered.add(arg.value.id)
    rows = []
    for alias in sorted(registered):
        module = modules[alias]
        file = ROOT / 'src' / Path(*module.split('.')).with_suffix('.py')
        content = ast.parse(file.read_text(encoding='utf-8'))
        prefix = ''
        for node in content.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if getattr(node.value.func, 'id', '') == 'APIRouter':
                    prefix = next((literal(k.value) for k in node.value.keywords if k.arg == 'prefix'), '')
        for node in content.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                    continue
                method = deco.func.attr
                if method not in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}:
                    continue
                path = prefix + (literal(deco.args[0]) if deco.args else '')
                rows.append({'method': method.upper(), 'path': path or '/',
                             'module': module.removeprefix('secretaria.'), 'function': node.name,
                             'source': file.relative_to(ROOT).as_posix(), 'line': node.lineno})
    return sorted(rows, key=lambda r: (r['path'], r['method']))

ROUTES = inventory()
(OUT / 'inventario-apis.json').write_text(json.dumps({'commit': SHA, 'date': '2026-10-28', 'routes': ROUTES}, ensure_ascii=False, indent=2), encoding='utf-8')

CSS = r'''
@page { size: A4; margin: 0; }
:root{--ink:#122d32;--teal:#006b60;--mint:#a9ecd5;--paper:#f5f3ed;--gray:#59696a;--line:#d8dfda;--gold:#c8893b}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#dce3df;color:var(--ink);font-family:'Segoe UI',Arial,sans-serif;font-size:14px;line-height:1.5}
nav{position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;align-items:center;padding:12px 22px;background:var(--ink);color:white;box-shadow:0 4px 16px #0002}
nav a{color:var(--mint);margin-left:18px;text-decoration:none;font-size:12px}button{border:0;border-radius:5px;padding:8px 13px;background:var(--mint);color:var(--ink);cursor:pointer;font-weight:600}
.sheet{position:relative;width:210mm;height:297mm;margin:22px auto;background:var(--paper);padding:19mm 17mm 17mm;overflow:hidden;box-shadow:0 12px 40px #1c34351c;break-after:page;page-break-after:always}
.sheet:last-child{break-after:auto;page-break-after:auto}.top{display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:26px;font-size:10px;letter-spacing:1.8px;text-transform:uppercase;color:var(--gray)}
.brand{font-weight:800;color:var(--teal);letter-spacing:1px}.foot{position:absolute;bottom:10mm;left:17mm;right:17mm;display:flex;justify-content:space-between;color:var(--gray);font-size:9px;border-top:1px solid var(--line);padding-top:9px}
.eyebrow{font-size:11px;letter-spacing:2.4px;color:var(--teal);text-transform:uppercase;font-weight:700;margin:0 0 10px}h1{font-size:69px;line-height:1.03;letter-spacing:-3px;font-weight:750;margin:0 0 22px}h2{font-size:36px;line-height:1.1;letter-spacing:-1.3px;margin:0 0 16px;font-weight:720}h3{font-size:17px;line-height:1.3;margin:0 0 9px}h4{font-size:13px;margin:0 0 6px}p{margin:0 0 13px}.lead{font-size:17px;line-height:1.6;color:#3e5658;margin-bottom:24px}.small{font-size:11px;color:var(--gray)}.tiny{font-size:10px;color:var(--gray)}.source{font-size:10px;color:var(--gray);margin-top:18px;padding-top:10px;border-top:1px solid var(--line)}.source a,a{color:var(--teal)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:13px}.card{background:white;border:1px solid var(--line);padding:18px;border-radius:9px}.card.dark{background:var(--ink);color:white;border:0}.card.dark p,.card.dark .small{color:#d2e4df}.card.mint{background:#e4f3e9;border-color:#bcdcca}.num{font-size:35px;line-height:1.2;letter-spacing:-1px;font-weight:750;color:var(--teal);margin:6px 0}.dark .num{color:var(--mint)}.label{font-size:11px;letter-spacing:1px;text-transform:uppercase}.note{background:#e6efe9;border-left:4px solid var(--teal);padding:15px 17px;margin:19px 0;font-size:13px}.note.gold{background:#f6ecdc;border-color:var(--gold)}.tag{display:inline-block;padding:3px 8px;border-radius:20px;background:#dceee4;color:var(--teal);font-size:10px;font-weight:650}.tag.gold{background:#f3e6cf;color:#895b21}.divider{height:1px;background:var(--line);margin:22px 0}
table{width:100%;border-collapse:collapse;font-size:12px;margin:16px 0}th{text-align:left;padding:10px 8px;background:#e4e9e2;color:#395452;font-size:10px;text-transform:uppercase;letter-spacing:.7px}td{padding:11px 8px;vertical-align:top;border-bottom:1px solid var(--line)}td:first-child{font-weight:650}code{font-family:Consolas,monospace;font-size:11px;overflow-wrap:anywhere}ul,ol{padding-left:19px;margin:8px 0 15px}li{margin-bottom:8px}.mono{font-family:Consolas,monospace}.wide{grid-column:1/-1}.mt{margin-top:20px}.no-margin{margin:0}
.cover{background:var(--ink);color:white}.cover .top,.cover .foot{color:#a8c0b9;border-color:#44605e}.cover .brand,.cover .eyebrow{color:var(--mint)}.cover .lead{color:#d2ded9;max-width:550px}.cover .orb{position:absolute;width:370px;height:370px;border:1px solid #4c8171;border-radius:50%;right:-165px;top:100px}.cover .orb:after{content:'';position:absolute;width:240px;height:240px;border:1px solid #4c8171;border-radius:50%;top:65px;left:65px}.cover .hero{margin-top:38px;padding:26px 25px;border:1px solid #52736a;border-radius:10px;background:#1e3d40}.cover .hero .value{font-size:64px;line-height:1.1;color:var(--mint);font-weight:750;letter-spacing:-2px}.cover .hero .small{color:#c5d9d2}.cover-bottom{display:flex;gap:20px;margin-top:28px}.cover-bottom div{flex:1;border-top:1px solid #52736a;padding-top:13px;font-size:12px;color:#c2d5ce}
.step{display:grid;grid-template-columns:36px 1fr 90px;gap:13px;border-bottom:1px solid var(--line);padding:14px 0}.step b.index{display:flex;width:32px;height:32px;align-items:center;justify-content:center;border-radius:50%;background:var(--ink);color:var(--mint);font-size:13px}.step p{font-size:12px;margin:4px 0 0}.step .tag{align-self:start;text-align:center}.arch{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.node{background:white;border:1px solid var(--line);border-radius:7px;padding:15px;text-align:center;min-height:82px}.node b{display:block;font-size:14px}.node small{display:block;font-size:10px;color:var(--gray);margin-top:5px}.node.core{background:var(--ink);color:white}.node.core small{color:#d1e2dc}.arrow{text-align:center;font-size:18px;color:var(--teal);grid-column:1/-1;line-height:1}.span3{grid-column:1/-1}.flowpair{display:grid;grid-template-columns:1fr 35px 1fr;gap:10px;align-items:center;margin:18px 0}.flowpair>span{text-align:center;font-size:23px;color:var(--teal)}
.barrow{display:grid;grid-template-columns:120px 1fr 83px;gap:15px;align-items:center;margin:18px 0;font-size:12px}.bartrack{height:28px;background:#e0e6de;border-radius:4px;overflow:hidden}.bar{height:100%;background:var(--teal);min-width:3px;border-radius:4px}.bar.alt{background:#ca934f}.formula{font-family:Consolas,monospace;padding:18px;background:var(--ink);color:var(--mint);border-radius:8px;font-size:14px;line-height:1.8;margin:17px 0}.simgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.field label{display:block;font-size:10px;color:var(--gray);margin-bottom:5px}.field input{width:100%;font:600 15px 'Segoe UI',sans-serif;border:1px solid #b7c7bf;border-radius:4px;padding:9px;background:white;color:var(--ink)}.simresult{margin:20px 0;display:grid;grid-template-columns:1fr 1fr;gap:14px}.simresult .num{font-size:37px}.route-table{font-size:10px;line-height:1.35;table-layout:fixed}.route-table th{padding:8px 6px;font-size:9px}.route-table td{padding:6px;border-bottom:1px solid #dce2dc}.route-table td:nth-child(1){width:12%;font-size:9px}.route-table td:nth-child(2){width:59%}.route-table td:nth-child(3){width:29%}.route-table code{font-size:10px;word-break:break-word}.toc a{display:flex;justify-content:space-between;text-decoration:none;padding:9px 0;border-bottom:1px solid var(--line);font-size:12px}.ref{font-size:11px;margin-bottom:13px}.ref b{display:block;margin-bottom:3px}.ref code{font-size:10px}.ref a{overflow-wrap:anywhere}.status{display:grid;grid-template-columns:165px 1fr;gap:15px;padding:13px 0;border-bottom:1px solid var(--line);font-size:12px}.status p{margin:0}.balance{font-size:23px;line-height:1.4;font-weight:650;letter-spacing:-.5px}.print-only{display:none}
/* Ajustes editoriais por página: preservar leitura e respiro antes do rodapé. */
.cover .orb{right:0;width:240px;height:240px;top:80px;opacity:.45}.cover .orb:after{width:150px;height:150px;top:44px;left:44px}
#arquitetura .arch{gap:8px}#arquitetura .node{min-height:61px;padding:10px}#arquitetura .node small{line-height:1.3}#arquitetura .node b{font-size:13px}#arquitetura .arrow{font-size:16px}#arquitetura .lead{margin-bottom:17px}#arquitetura .note{margin:14px 0}
#apis table{table-layout:fixed;font-size:11.5px;line-height:1.4}#apis th:nth-child(1){width:22%}#apis th:nth-child(2){width:40%}#apis th:nth-child(3){width:38%}#apis td{padding:8px 7px}#apis code{font-size:10.5px}#apis .card{padding:14px;font-size:12px}#apis .card h3{font-size:15px}#apis .lead{margin-bottom:16px}
#dados .card{font-size:13px;padding:16px}#dados .note{margin:13px 0}
#cenarios td{padding:8px}#cenarios .barrow{margin:12px 0}#cenarios .bartrack{height:23px}#cenarios .note{margin:14px 0}
#operacao table{font-size:11.5px;line-height:1.4;margin:12px 0}#operacao td{padding:6px 8px}#operacao .card{padding:14px}#operacao .card p{font-size:11px}#operacao .lead{margin-bottom:14px;font-size:16px;line-height:1.45}#operacao .note{margin:13px 0;font-size:12px}#operacao .formula{padding:14px;font-size:13px;line-height:1.6}
@media print{body{background:white}nav,.screen-only{display:none!important}.sheet{margin:0;box-shadow:none}.print-only{display:block}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}.simgrid input{border-color:#c7d4cb}a{text-decoration:none}}
@media screen and (max-width:820px){nav{position:relative;flex-wrap:wrap;gap:8px}nav a{margin-left:10px}.sheet{width:100%;height:auto;min-height:297mm;margin:0;padding:30px 23px 70px;overflow:visible;box-shadow:none;border-bottom:12px solid #dce3df}.foot{left:23px;right:23px}.cover h1{font-size:58px}.grid3,.simgrid{grid-template-columns:1fr 1fr}.cover .hero .value{font-size:49px}.route-table code{font-size:9px}}
@media screen and (max-width:600px){.sheet{overflow:hidden}h2{font-size:31px}.grid2,.grid3,.arch,.simresult{grid-template-columns:1fr}.span3{grid-column:auto}.node{min-width:0}.grid2>*{min-width:0}.simgrid{grid-template-columns:1fr 1fr}.field{min-width:0}.cover .orb{display:none}.cover h1{font-size:52px}.cover-bottom{gap:12px}.cover-bottom div{font-size:10px}.step{grid-template-columns:30px 1fr;gap:10px}.step .tag{grid-column:2;justify-self:start}.barrow{grid-template-columns:65px 1fr 76px;gap:8px}.status{grid-template-columns:1fr;gap:6px}.formula{font-size:11px;overflow-wrap:anywhere}table{font-size:10px;table-layout:fixed}td,th{padding:7px 4px;overflow-wrap:anywhere}code{font-size:9px}#apis table{font-size:10px}#apis code{font-size:9px}.route-table{font-size:9px}nav a{font-size:10px}.cover .hero{padding:20px 17px}}
'''

PAGES = []
def page(slug, section, title, subtitle, content, cls=''):
    n = len(PAGES) + 1
    top = '<div class="top"><span class="brand">BRAIN / SecretarIA</span><span>Dossiê de arquitetura e economia</span></div>'
    heading = f'<p class="eyebrow">{section}</p><h2>{title}</h2><p class="lead">{subtitle}</p>' if title else ''
    PAGES.append(f'<section class="sheet {cls}" id="{slug}">{top}{heading}{content}<div class="foot"><span>28 OUT 2026 · Código local + fontes oficiais · Sem auditoria de produção</span><span>{n:02d}</span></div></section>')

page('inicio', '', '', '', '''
<div class="orb"></div><p class="eyebrow" style="margin-top:48px">O atendimento por dentro</p>
<h1>Secretar<span style="color:var(--mint)">IA</span>.<br>Da conversa<br>à consulta.</h1>
<p class="lead">Como a clínica recebe, orienta e agenda.<br>A arquitetura que sustenta a experiência.<br>E quanto a inteligência realmente custa.</p>
<div class="hero"><div class="label">Previsão central · somente LLM por consulta marcada</div><div class="value">R$ 0,0125</div><p style="margin:10px 0 9px">Cerca de <b>1,25 centavo</b> por agendamento.</p><div class="small">Cenário ilustrativo com Haiku 5.5, 35% das conversas com IA e 60% de conversão. Inclui o gasto das conversas que não agendam. Não é média observada nem custo total da operação.</div></div>
<div class="cover-bottom"><div><b>01 / EXPERIÊNCIA</b><br>Fluxo guiado e conversa livre</div><div><b>02 / ENGENHARIA</b><br>Serviços, dados e APIs</div><div><b>03 / ECONOMIA</b><br>Premissas e simulador</div></div>
<p class="tiny" style="color:#adc6bc;margin-top:26px">Base: checkout local de 28/10/2026. Valores em reais usam dólar hipotético de R$ 5,50; sem tributos e spread. Documento sobre agendamento, sem estimativa do custo do atendimento médico.</p>
''', 'cover')

page('leitura', '01 / A tese', 'Automação como base.<br>IA quando faz diferença.',
     'A SecretarIA combina regras de agendamento com linguagem natural. Essa divisão explica tanto o funcionamento quanto a economia.', '''
<div class="grid2"><div class="card dark"><div class="label">Caminho guiado</div><h3 style="margin-top:12px">Escolher e confirmar</h3><p>Botões, listas, perguntas fixas, catálogo e horários conduzem o paciente. O servidor valida escolhas e executa as ações.</p><div class="num">R$ 0</div><p class="small no-margin">de tokens de chat, se toda a jornada permanecer nesse caminho. Infraestrutura e canal continuam existindo.</p></div>
<div class="card"><div class="label">Caminho conversacional</div><h3 style="margin-top:12px">Entender e orientar</h3><p>A IA responde a texto livre, usa os fatos da clínica e chama ferramentas permitidas. Pode devolver o paciente ao fluxo guiado.</p><div class="num">Sob demanda</div><p class="small no-margin">Uma fala do paciente pode provocar várias requisições ao modelo, especialmente ao consultar ferramentas.</p></div></div>
<div class="note"><b>A unidade econômica é a consulta marcada.</b> Para saber quanto cada uma custa, some também a IA consumida por quem tirou dúvidas, desistiu ou precisou de uma pessoa da equipe.</div>
<div class="grid2 mt"><div><h3>O que a clínica configura</h3><p>Profissionais, serviços, preços, convênios, duração, expediente, fuso, orientações, contexto, agenda e política de sinal.</p><h3 style="margin-top:22px">O que o paciente percebe</h3><p>Uma conversa com continuidade: pode escolher, perguntar, voltar, alterar dados e gerenciar a própria consulta.</p></div><div class="toc"><h3>Roteiro de leitura</h3><a href="#jornada">A jornada e a entrada da IA <b>03</b></a><a href="#arquitetura">Mapa da arquitetura <b>04</b></a><a href="#integracoes">Integrações e APIs <b>06</b></a><a href="#custo">A conta do Haiku <b>08</b></a><a href="#simulador">Simulador ajustável <b>10</b></a><a href="#evidencias">Fontes e limites <b>13</b></a></div></div>
<div class="source">Código confirmado: services/flow_router.py; workers/turn_router.py; workers/orchestrator.py; ai/graph.py; ai/scoped_help.py. O documento descreve o código encontrado; disponibilidade ao vivo depende de implantação e configuração.</div>
''')

page('jornada', '02 / Experiência do paciente', 'Uma jornada.<br>Dois modos de atender.', 'WhatsApp e Portal têm entradas diferentes e compartilham o núcleo de regras de agendamento.', '''
<div class="step"><b class="index">1</b><div><h4>Entrar e identificar</h4><p>WhatsApp recebe a mensagem da pessoa. O Portal recebe a visita ou a mensagem pelo brain-api. Nome, consentimento e verificação seguem a política do canal e da conta.</p></div><span class="tag">Sem chat LLM</span></div>
<div class="step"><b class="index">2</b><div><h4>Escolher o caminho</h4><p>Saudação e menu reconhecem o contexto disponível. “Outro” pode começar com uma pergunta fixa; a IA entra na resposta livre da pessoa.</p></div><span class="tag">Regras + IA</span></div>
<div class="step"><b class="index">3</b><div><h4>Montar o agendamento</h4><p>Beneficiário, convênio, profissional e serviço compõem a escolha, conforme configuração. Dados já informados podem ser preservados como rascunho.</p></div><span class="tag">Guiado</span></div>
<div class="step"><b class="index">4</b><div><h4>Escolher data e horário</h4><p>Expediente, duração, bloqueios e ocupação real determinam as opções. A IA ajuda a interpretar a intenção; disponibilidade vem do serviço de agenda.</p></div><span class="tag">Agenda real</span></div>
<div class="step"><b class="index">5</b><div><h4>Revisar, verificar e confirmar</h4><p>O sistema aplica os portões necessários: confirmação, identidade e, quando configurado, sinal. No modo Pix “antes”, há reserva temporária e a consulta nasce após identificar o pagamento.</p></div><span class="tag">Transacional</span></div>
<div class="step"><b class="index">6</b><div><h4>Acompanhar e gerenciar</h4><p>Confirmação, notificações, lembretes e ações de remarcar/cancelar são orquestrados pelo servidor. A clínica pode assumir o atendimento humano.</p></div><span class="tag">Automação</span></div>
<div class="note gold"><b>Nem todo texto livre usa a mesma IA.</b> Há ajuda delimitada para escolher profissional/serviço e há o agente completo, com histórico e ferramentas. Ambos podem gerar tokens; clicar em um botão não implica uma chamada por si só.</div>
<div class="source">Fontes: flow_router.py; ai/scoped_help.py; services/booking_promotion.py; services/payments/fulfillment.py; workers/portal/; workers/whatsapp/. Ordem e portões dependem das opções habilitadas. Não se reproduziu uma jornada de produção nesta análise.</div>
''')

page('arquitetura', '03 / Visão de sistema', 'O mapa que conecta<br>paciente, clínica e agenda.', 'A API recebe. A fila desacopla. O worker decide e executa. O banco preserva o estado.', '''
<div class="arch">
<div class="node"><b>Paciente / WhatsApp</b><small>Meta Cloud API · Coexistence</small></div><div class="node"><b>Paciente / Portal</b><small>Brain-Message-Frontend</small></div><div class="node"><b>Equipe / Portal</b><small>Console e configuração</small></div>
<div class="arrow">↓ WhatsApp direto &nbsp;&nbsp;&nbsp; ↓ Portais via brain-api &nbsp;&nbsp;&nbsp; ↓</div>
<div class="node core span3"><b>SecretarIA API · FastAPI</b><small>Entrada HTTP · autenticação/validação · consultas do painel · enfileiramento</small></div>
<div class="node span3"><b>Redis + ARQ</b><small>Jobs assíncronos · deduplicação e estado temporário · caches</small></div>
<div class="arrow">↓ processar e responder</div>
<div class="node core span3"><b>Worker SecretarIA · processo independente</b><small>Entrada por canal → identidade / consentimento / humano → roteador de turno</small></div>
<div class="node"><b>Fluxo guiado</b><small>Regras e escolhas validadas</small></div><div class="node"><b>Agente LangGraph</b><small>Haiku + ferramentas</small></div><div class="node"><b>Jobs e plugins</b><small>Lembretes · avisos · sinal</small></div>
<div class="arrow">↓ serviços de negócio compartilhados</div>
<div class="node"><b>Agenda</b><small>Nativa em Postgres OU Google Calendar, por credencial/escopo</small></div><div class="node"><b>PostgreSQL</b><small>Pacientes · conversas · consultas · consentimentos · pagamentos</small></div><div class="node"><b>Conexões externas</b><small>Meta · Anthropic · SMTP · Asaas · R2 · transcrição</small></div>
</div>
<div class="note"><b>brain-api é o centro do ecossistema.</b> Identidade, assinatura, permissões e intermediação entre produtos passam por ele. O encaminhamento ao PreCheck sai da SecretarIA para o brain-api, que coordena o outro produto.</div>
<p class="small"><b>A agenda já pode ser nativa.</b> Sem credencial Google aplicável, o código seleciona a agenda local. Uma credencial Google revogada gera falha e reconexão; ela não troca silenciosamente para a agenda local, o que evitaria separar reservas em duas agendas.</p>
<div class="source">Confirmado em main.py; workers/arq_worker.py; services/calendar.py::from_tenant_config; local_calendar.py; services/precheck.py; config.py. Papel dos demais repositórios: PRODUCTS.md; código externo não auditado nesta entrega.</div>
''')

page('dados', '04 / Estado, segurança e responsabilidade', 'O que precisa continuar<br>verdade entre mensagens.', 'O modelo entende a fala. O estado persistido e os serviços determinam o que de fato aconteceu.', '''
<table><thead><tr><th>Domínio</th><th>O que guarda</th><th>Por que importa</th></tr></thead><tbody>
<tr><td>Clínica e profissionais</td><td>Configuração, catálogo, horários, unidades e credenciais separadas.</td><td>O atendimento usa a oferta real de cada clínica.</td></tr>
<tr><td>Paciente e conversa</td><td>Identidade do canal, mensagens, etapa e modo bot/humano.</td><td>Permite retomar sem reiniciar a conversa.</td></tr>
<tr><td>Consulta e rascunho</td><td>Escolhas em andamento, consulta efetiva, reserva temporária e agenda.</td><td>Uma intenção ainda não é uma consulta marcada.</td></tr>
<tr><td>Consentimentos</td><td>Registro de aceites e eventos de consentimento.</td><td>Documenta autorização dentro do fluxo.</td></tr>
<tr><td>Lembretes e entrega</td><td>Agendamento de avisos e estados de mensagens.</td><td>Ajuda a evitar repetição e acompanhar comunicação.</td></tr>
<tr><td>Sinal e conciliação</td><td>Cobrança, evento recebido, reservas e pendências financeiras.</td><td>Pagamento depende do provedor, não da fala da IA.</td></tr>
</tbody></table>
<div class="grid2"><div class="card"><h3>Antes da LLM</h3><p>Histórico e resultados de ferramentas passam por pseudonimização. Identificadores conhecidos recebem marcadores; a resposta é reidratada antes do envio.</p><p class="small no-margin">Isso reduz exposição; não equivale a anonimização completa ou prova de conformidade LGPD.</p></div><div class="card"><h3>Dentro da plataforma</h3><p>Segredos de integração são cifrados. Acesso interno e painel têm controles próprios. O contexto e as consultas devem respeitar clínica, papel e dono da agenda.</p><p class="small no-margin">Nenhuma credencial, conversa real ou dado pessoal foi copiado para este documento.</p></div></div>
<div class="note"><b>O bot cede à equipe.</b> Coexistência e estado de atendimento humano impedem que o bot continue respondendo como se ainda fosse o responsável. Há rotinas de timeout e retomada.</div>
<div class="source">models/__init__.py; models/conversation.py; services/pii_pseudonymization.py; ai/pii.py; ai/tool_output.py; services/sensitive_claim_guard.py; core/security.py; workers/shared/handover.py e workers/arq_worker.py. Existência de mecanismos não substitui auditoria de segurança em runtime.</div>
''')

page('integracoes', '05 / APIs e fornecedores', 'Cada conexão tem<br>um papel e uma conta.', 'A camada conversacional é só uma parte da operação. Há custos que não dependem de tokens.', '''
<table><thead><tr><th>Conexão</th><th>Uso na SecretarIA</th><th>Como considerar o custo</th></tr></thead><tbody>
<tr><td>Meta / WhatsApp Cloud API</td><td>Receber eventos, enviar texto, botões, listas, mídia e templates; coexistência com atendimento humano.</td><td>Mensagens elegíveis a cobrança por categoria, destino e tabela vigente. Lembretes podem ter cobrança mesmo sem LLM.</td></tr>
<tr><td>Anthropic / Messages API</td><td>Agente, ajuda delimitada e extração de fontes de contexto; padrão local Haiku 5.5.</td><td>Entrada, saída e cache. Requisições longas têm faixa própria.</td></tr>
<tr><td>Google Calendar + OAuth</td><td>Agenda externa por profissional ou conta compartilhada; disponibilidade e eventos.</td><td>Separar eventuais planos de conta e infraestrutura. O cenário não atribui tarifa fictícia por chamada.</td></tr>
<tr><td>OpenAI / Groq · áudio</td><td>Transcrição transforma a mensagem de voz em texto antes do fluxo normal.</td><td>Despesa de áudio separada da conversa Haiku. Modelo padrão local: gpt-4o-mini-transcribe.</td></tr>
<tr><td>SMTP</td><td>Códigos, convites, confirmação, avisos e comunicações operacionais.</td><td>Plano contratado e/ou preço por envio. Não requer tokens para textos fixos.</td></tr>
<tr><td>Cloudflare R2 / S3 API</td><td>Anexos do Portal, em armazenamento próprio da SecretarIA.</td><td>Armazenamento e operações; anexos não significam interpretação visual por IA.</td></tr>
<tr><td>Asaas / pagamentos</td><td>Sinal Pix, estado das cobranças e webhooks. A abstração admite outros provedores.</td><td>Tarifa de cobrança e condições da clínica. Não confundir valor do sinal com custo da plataforma.</td></tr>
<tr><td>brain-api / HTTP interno</td><td>Identidade, permissões, assinatura, provisionamento, Portal e ponte para PreCheck.</td><td>Rateio da plataforma compartilhada; PreCheck tem escopo e custos próprios.</td></tr>
</tbody></table>
<p class="small">A API de chat usa Anthropic quando a chave correspondente está configurada. Sem ela, com chave OpenAI disponível, o código usa OpenAI e registra o desvio. Não há troca automática por erro de Haiku para OpenAI nesse seletor.</p>
<div class="source">ai/chat_model.py; config.py; services/calendar.py; services/media_storage.py; services/email.py; workers/whatsapp/audio.py; services/payments/; services/precheck.py. Regras de cobrança de mensagens: <a href="https://whatsappbusiness.com/products/platform-pricing/">Meta · Pricing</a> (consulta em 28/10/2026). Valores contratados não foram inspecionados.</div>
''')

page('apis', '06 / Superfícies HTTP', 'A API organiza o produto<br>em fronteiras claras.', 'Mapa funcional das rotas da SecretarIA. O inventário completo extraído do código está no apêndice.', '''
<table><thead><tr><th>Superfície</th><th>Exemplos de rotas</th><th>Quem usa / finalidade</th></tr></thead><tbody>
<tr><td>Canal WhatsApp</td><td><code>GET /webhook<br>POST /webhook</code></td><td>Meta valida e entrega eventos. Assinatura é verificada; processamento pesado vai à fila.</td></tr>
<tr><td>Canal Portal</td><td><code>/internal/brain-message/*</code></td><td>brain-api encaminha entradas, abertura, mensagens e sinais do canal.</td></tr>
<tr><td>Configuração e catálogo</td><td><code>/tenants/me/configuration<br>/tenants/me/services<br>/tenants/me/professionals<br>/tenants/me/insurance-plans</code></td><td>Painel da clínica mantém oferta, contexto e profissionais.</td></tr>
<tr><td>Agenda</td><td><code>/tenants/me/calendar/events<br>/tenants/me/calendar/appointments<br>/tenants/me/calendar/blocks</code></td><td>Consultar e operar a agenda com escopo de acesso.</td></tr>
<tr><td>Conversas e equipe</td><td><code>/tenants/me/conversations<br>.../{conversation_id}/handover<br>.../{conversation_id}/messages</code></td><td>Atendimento humano, histórico e mensagens da equipe.</td></tr>
<tr><td>Integrações e operações</td><td><code>/oauth/*<br>/webhooks/*<br>/internal/*<br>/tenants/me/payment-*</code></td><td>Google, pagamentos, provisionamento, privacidade e operação entre serviços.</td></tr>
<tr><td>Saúde e observabilidade</td><td><code>/health<br>/build</code></td><td>Verificar processo e identidade de build. API e worker precisam estar na versão adequada.</td></tr>
</tbody></table>
<div class="grid2"><div class="card"><h3>Resposta rápida ao canal</h3><p>O webhook WhatsApp verifica, reduz o payload e enfileira. Se a fila não está disponível, devolve indisponibilidade para permitir reentrega.</p></div><div class="card"><h3>Pagamento exige durabilidade</h3><p>O webhook de pagamento por provedor persiste o evento antes de aceitar. A confirmação financeira é reconciliada com o provedor.</p></div></div>
<div class="source">main.py registra os routers. O apêndice combina os prefixos de APIRouter com seus decorators HTTP por AST, sem carregar a aplicação ou segredos. Rotas padrão do framework (/docs, /redoc, /openapi.json) não entram na contagem.</div>
''')

page('custo', '07 / Economia da inteligência', 'Quanto custa<br>uma chamada de Haiku?', 'Premissas de orçamento, com a versão encontrada no código: claude-haiku-5-5.', '''
<div class="grid3"><div class="card"><div class="label">Entrada / 1M tokens</div><div class="num">US$ 0,10</div><p class="small no-margin">Prompt por requisição até 100 mil tokens.</p></div><div class="card"><div class="label">Saída / 1M tokens</div><div class="num">US$ 0,50</div><p class="small no-margin">Usar a saída faturável completa, inclusive thinking quando cobrado.</p></div><div class="card"><div class="label">Câmbio ilustrativo</div><div class="num">R$ 5,50</div><p class="small no-margin">Por US$ 1. Não é cotação de mercado, nem inclui tributos/spread.</p></div></div>
<h3 class="mt">Uma chamada do cenário central</h3><table><thead><tr><th>Componente</th><th>Premissa</th><th>Custo em US$</th></tr></thead><tbody><tr><td>Entrada completa</td><td>8.000 tokens</td><td>0,0008</td></tr><tr><td>Saída faturável</td><td>1.000 tokens</td><td>0,0005</td></tr><tr><td>Total por requisição</td><td>Sem desconto de cache</td><td><b>0,0013 = R$ 0,00715</b></td></tr></tbody></table>
<div class="note"><b>“Mensagem” e “chamada” são diferentes.</b> Um turno pode gerar Haiku → ferramenta → Haiku. Cada requisição reenvia parte do contexto, das instruções e das definições das ferramentas; tudo isso deve entrar nos tokens de entrada.</div>
<div class="grid2"><div><h3>O que pode mudar a conta</h3><p>O código limita o histórico a 30 mensagens no agente e 10 na ajuda delimitada. Isso não é um limite de tokens. O teto local de saída Haiku é 16.000 por chamada, incluindo thinking; não representa consumo médio.</p></div><div><h3>Cache já está previsto</h3><p>O prompt do sistema recebe marca de cache efêmero. Orçamentamos sem hits: gravação de 5 min custa US$ 0,125/M e leitura US$ 0,01/M. Eficiência real depende da repetição do prefixo.</p></div></div>
<p class="small">Acima de 100 mil tokens de prompt por requisição, a tabela passa a US$ 0,50/M de entrada e US$ 2,50/M de saída. O limite é por requisição, não pela soma do mês. Os cenários seguintes usam a faixa menor.</p>
<div class="source">Tarifas: <a href="https://platform.claude.com/docs/en/about-claude/pricing">Anthropic · Pricing</a>; modelo: <a href="https://platform.claude.com/docs/en/models/haiku-5-5/overview">Haiku 5.5</a> (28/10/2026). Código: config.py; ai/chat_model.py; ai/graph.py; ai/scoped_help.py. Tokens são hipóteses, sem medição de produção.</div>
''')

page('cenarios', '08 / Custo por resultado', 'Da chamada barata<br>à consulta marcada.', 'Uma previsão útil considera ativação da IA, volume de chamadas e conversão em agendamento.', '''
<div class="formula">Custo LLM / consulta = (p ÷ q) × k × c<br>c = câmbio × [(entrada × tarifa_in + saída × tarifa_out) ÷ 1.000.000]</div>
<p class="small"><b>p:</b> fração das conversas com IA. <b>q:</b> consultas novas marcadas por conversa iniciada (simplificação do cenário: no máximo uma). <b>k:</b> requisições médias de modelo por conversa com IA, somando todos os turnos e loops. <b>c:</b> custo médio de uma requisição. Conversas sem agendamento permanecem no numerador.</p>
<table><thead><tr><th>Premissa</th><th>Leve</th><th>Central</th><th>Intensivo</th></tr></thead><tbody>
<tr><td>Conversas com IA (p)</td><td>15%</td><td>35%</td><td>65%</td></tr><tr><td>Conversão (q)</td><td>75%</td><td>60%</td><td>40%</td></tr><tr><td>Requisições (k)</td><td>2</td><td>3</td><td>6</td></tr><tr><td>Tokens / requisição</td><td>4 mil in + 500 out</td><td>8 mil in + 1 mil out</td><td>16 mil in + 2 mil out</td></tr><tr><td>LLM / consulta</td><td><b>R$ 0,00143</b></td><td><b>R$ 0,01251</b></td><td><b>R$ 0,13943</b></td></tr><tr><td>LLM / 1.000 consultas</td><td>R$ 1,43</td><td>R$ 12,51</td><td>R$ 139,43</td></tr></tbody></table>
<h3>Escala de despesa para 1.000 consultas</h3><div class="barrow"><span>Leve</span><div class="bartrack"><div class="bar" style="width:1.03%"></div></div><b>R$ 1,43</b></div><div class="barrow"><span>Central</span><div class="bartrack"><div class="bar" style="width:8.97%"></div></div><b>R$ 12,51</b></div><div class="barrow"><span>Intensivo</span><div class="bartrack"><div class="bar alt" style="width:100%"></div></div><b>R$ 139,43</b></div>
<div class="note"><b>Orçamento central com reserva de 30%:</b> aproximadamente <b>R$ 0,01627 por consulta</b> ou <b>R$ 16,27 por 1.000</b>, somente LLM. A reserva é uma hipótese de planejamento para variações, gravações de cache e repetições; não é teto garantido.</div>
<div class="source">Cálculos próprios com tarifas da página anterior. Não são intervalos estatísticos, amostra real ou promessa de preço. No central: US$ 0,0013 × 3 × 5,50 × (0,35/0,60) = R$ 0,0125125.</div>
''')

page('simulador', '09 / Laboratório de custos', 'Mude as premissas.<br>Veja o efeito na hora.', 'Na versão HTML, os campos recalculam o orçamento localmente. No PDF, esta página registra o cenário central.', '''
<div class="simgrid">
<div class="field"><label for="bookings">Consultas marcadas / mês</label><input id="bookings" type="number" value="1000" min="1" step="1"></div>
<div class="field"><label for="activation">Conversas com IA (%)</label><input id="activation" type="number" value="35" min="0" max="100"></div>
<div class="field"><label for="conversion">Conversão em consulta (%)</label><input id="conversion" type="number" value="60" min="0.1" max="100" step="0.1"></div>
<div class="field"><label for="calls">Requisições / conversa com IA</label><input id="calls" type="number" value="3" min="0" step="0.1"></div>
<div class="field"><label for="inputTokens">Tokens de entrada / requisição</label><input id="inputTokens" type="number" value="8000" min="0" step="100"></div>
<div class="field"><label for="outputTokens">Tokens de saída / requisição</label><input id="outputTokens" type="number" value="1000" min="0" step="100"></div>
<div class="field"><label for="fx">R$ / US$ (hipótese)</label><input id="fx" type="number" value="5.5" min="0.01" step="0.01"></div>
<div class="field"><label for="reserve">Reserva de orçamento (%)</label><input id="reserve" type="number" value="30" min="0"></div>
<div class="field"><label for="fixed">Custos fixos rateados / mês (R$)</label><input id="fixed" type="number" value="0" min="0" step="10"></div>
</div>
<div class="simresult"><div class="card dark"><div class="label">LLM / consulta · sem reserva</div><div class="num" id="unitCost">R$ 0,01251</div><p class="small no-margin" id="unitNote">Inclui conversas sem agendamento.</p></div><div class="card mint"><div class="label">LLM / mês · com reserva</div><div class="num" id="monthlyCost">R$ 16,27</div><p class="small no-margin" id="monthlyNote">1.000 consultas marcadas.</p></div></div>
<div class="card"><h3>O funil implícito neste cenário</h3><p id="funnel" class="no-margin">1.667 conversas iniciadas → 583 com IA → 1.750 requisições → 1.000 consultas.</p><div class="divider" style="margin:14px 0"></div><p class="small no-margin" id="allocated">LLM com reserva + fixos informados: R$ 0,01627 por consulta. Outros custos variáveis não incluídos.</p></div>
<div class="note gold"><b>“Fixos = 0” significa não informado.</b> Preencha o rateio real de hospedagem, banco, Redis, observabilidade e plataforma. O total mostrado ainda não inclui mensagens cobradas, áudio, e-mail, anexos, pagamentos, suporte ou tributos.</div>
<p class="small" id="tierNote">Faixa usada: prompts até 100.000 tokens. Sem desconto de cache; sem tributos e spread.</p>
<button class="screen-only" onclick="resetSimulation()">Restaurar cenário central</button>
<div class="source">Simulador offline, sem envio de dados, sem analytics e sem dependências externas. Ao alterar a entrada acima de 100 mil tokens, aplica a faixa maior do Haiku 5.5. Valores médios não modelam uma mistura de requisições nas duas faixas.</div>
''')

page('operacao', '10 / O custo que o token não mostra', 'O custo total depende<br>da operação inteira.', 'A economia da LLM não torna a consulta gratuita. O custo total exige volumes e contratos reais.', '''
<div class="formula">Custo total / consulta =<br>LLM + canal + áudio + e-mail + arquivos + pagamentos<br>+ [(infra + plataforma + suporte) ÷ consultas marcadas]</div>
<table><thead><tr><th>Camada</th><th>Como medir</th><th>Tratamento neste estudo</th></tr></thead><tbody>
<tr><td>LLM de paciente</td><td>Soma de requisições, cache e saída faturável.</td><td>Estimativa explícita em três cenários.</td></tr>
<tr><td>LLM administrativa</td><td>Extração/preparação de fontes da clínica.</td><td>Separar como onboarding/manutenção e depois ratear.</td></tr>
<tr><td>Mensageria</td><td>Mensagens efetivamente cobradas × tarifa aplicável.</td><td>Sem tarifa Brasil inventada. Gratuidade depende da categoria e janela vigente.</td></tr>
<tr><td>Áudio e anexos</td><td>Uso de transcrição, armazenamento e operações.</td><td>Não entram na conta Haiku.</td></tr>
<tr><td>E-mail</td><td>Envios e plano contratado.</td><td>Valor contratual não informado.</td></tr>
<tr><td>Pagamentos</td><td>Cobranças e tarifa do provedor.</td><td>Separar quem absorve a tarifa: clínica ou plataforma.</td></tr>
<tr><td>Infra + equipe</td><td>API, worker, Postgres, Redis, monitoramento e suporte.</td><td>Rateio por volume; requer despesas reais.</td></tr>
</tbody></table>
<div class="grid2"><div class="card"><div class="label">Exemplo aritmético · não é cotação</div><div class="num">R$ 0,5163</div><p class="small">Por consulta com 1.000/mês, se o rateio fixo hipotético for R$ 500 e a LLM central com reserva custar R$ 16,27/mês. Demais variáveis excluídas.</p></div><div class="card"><div class="label">Mesmo fixo · 10.000 consultas/mês</div><div class="num">R$ 0,0663</div><p class="small">R$ 500 ÷ 10.000 + R$ 0,01627 de LLM com reserva. Mantém o fixo por hipótese; capacidade real pode exigir expansão.</p></div></div>
<div class="note"><b>O sinal Pix não é receita automática da SecretarIA.</b> É valor pago pelo paciente à clínica, conforme a configuração e a conta do provedor. A tarifa de recebimento e a mensalidade do SaaS são contas diferentes.</div>
<div class="source">Componentes observados em config.py, main.py, workers/arq_worker.py e serviços de integrações. Exemplo de R$ 500 é hipotético e não representa fatura, plano recomendado ou capacidade garantida.</div>
''')

page('gestao', '11 / Confiabilidade e próximos indicadores', 'Acompanhe o resultado.<br>Depois calibre a previsão.', 'A melhor decisão é preservar a jornada guiada e medir o gasto de todas as saídas do funil.', '''
<div class="status"><b>01 · Duas versões em operação</b><p>API e worker são serviços separados. Uma API saudável não prova que o worker está na mesma versão. A identidade de build e a comparação de paridade existem para detectar essa diferença.</p></div>
<div class="status"><b>02 · Falha não pode virar sucesso fictício</b><p>Há proteções de saída, escopo de ferramentas, tratamento de agenda indisponível e resposta fixa quando o turno não entrega texto. Não equivalem a garantia absoluta contra erro do modelo.</p></div>
<div class="status"><b>03 · Consultar o provedor é essencial</b><p>Reserva, cobrança criada e pagamento identificado são etapas distintas. Webhooks duplicados e reconciliação precisam ser tratados sem criar outra consulta ou outra cobrança.</p></div>
<div class="status"><b>04 · Métrica já disponível, com lacunas</b><p>O agente registra modelo, chamadas, ferramentas, tokens e tempo em <code>llm_turn_trace</code>; a ativação registra motivo. A ajuda delimitada e a extração não têm, neste estudo, cobertura financeira equivalente comprovada.</p></div>
<h3 class="mt">O painel mínimo para trocar hipótese por média</h3>
<div class="grid3"><div class="card"><h4>Conversão</h4><p class="small no-margin">Conversas iniciadas, novas consultas, abandono e encaminhamento humano.</p></div><div class="card"><h4>Ativação</h4><p class="small no-margin">% com IA, motivo, requisições por conversa e retorno aos botões.</p></div><div class="card"><h4>Conta real</h4><p class="small no-margin">Entrada normal, cache gravado/lido, saída e thinking já incluído na saída.</p></div></div>
<div class="note"><b>Média observada = gasto atribuído à coorte ÷ consultas novas marcadas pela coorte.</b> Uma remarcação não é outra aquisição. Deduplicar reentregas e amarrar conversas às consultas evita custo artificialmente baixo. Também acompanhar custo por consulta comparecida.</div>
<p class="small">Sugestão de medição: janela inicial de 30 dias, com corte por clínica e canal. Fechar a coorte após prazo definido para agendamento tardio. Conciliar com faturamento do fornecedor e incluir chamadas que falham, retries faturados e saídas por ferramentas. Separar uso administrativo e PreCheck.</p>
<div class="source">ai/trace.py e ai/graph.py::_log_agent_trace; workers/shared/llm_context.py; core/build_info.py; services/payments/. Leitura do código não comprova completude de logs nem consumo faturado. Instrumentação adicional foi recomendada, não implementada.</div>
''')

local_refs = [
 ('S01 · Entrada e implantação', 'src/secretaria/main.py; src/secretaria/api/webhook.py; src/secretaria/workers/arq_worker.py', 'API, webhook, fila, worker e rotinas agendadas.'),
 ('S02 · Jornada e decisão', 'src/secretaria/services/flow_router.py; src/secretaria/workers/turn_router.py; src/secretaria/workers/orchestrator.py', 'Fluxo guiado, delegação à IA e efeitos do turno.'),
 ('S03 · Modelo e tokens', 'src/secretaria/config.py; src/secretaria/ai/chat_model.py; src/secretaria/ai/graph.py; src/secretaria/ai/trace.py; src/secretaria/ai/scoped_help.py', 'Haiku 5.5, seleção por chave, cache, histórico e rastros de uso.'),
 ('S04 · Dados, agenda e privacidade', 'src/secretaria/models/; src/secretaria/services/calendar.py; src/secretaria/services/local_calendar.py; src/secretaria/ai/pii.py; src/secretaria/services/pii_pseudonymization.py', 'Estado, escolha de backend e pseudonimização.'),
 ('S05 · Integrações', 'src/secretaria/services/media_storage.py; src/secretaria/services/email.py; src/secretaria/services/precheck.py; src/secretaria/workers/whatsapp/audio.py; src/secretaria/services/payments/', 'Anexos, e-mail, áudio, PreCheck e sinal.'),
 ('S06 · Contexto operacional', '../PRODUCTS.md; AGENTS.md; CLAUDE.md; docs/CHECKPOINT_llm_provider_claude.md; docs/CHECKPOINT_sinal_padrao.md', 'Mapa entre produtos e histórico de decisões. Checkpoints podem descrever estados anteriores ao HEAD atual.'),
]
refs_html = ''.join(f'<div class="ref"><b>{E(a)}</b><code>{E(b)}</code><br>{E(c)}</div>' for a,b,c in local_refs)
page('evidencias', '12 / Evidência e leitura correta', 'O que foi verificado.<br>O que ainda é hipótese.', 'Este dossiê combina engenharia confirmada no checkout com planejamento econômico parametrizado.', f'''
<div class="note" style="margin-top:0"><b>Base reprodutível:</b> repositório SecretarIA, branch <code>main</code>, commit <code>{SHA[:12]}</code>, leitura em 28/10/2026. O Git já continha alterações do usuário em CLAUDE.md e AGENTS.md; elas foram preservadas. Graphify estava STALE e foi usado apenas como navegação histórica.</div>
{refs_html}
<div class="ref"><b>W01 · Tarifas e modelo · consulta em 28/10/2026</b><a href="https://platform.claude.com/docs/en/about-claude/pricing">Anthropic · tabela oficial de preços</a> · <a href="https://platform.claude.com/docs/en/models/haiku-5-5/overview">Haiku 5.5 · referência do modelo</a></div>
<div class="ref"><b>W02 · Canal · consulta em 28/10/2026</b><a href="https://whatsappbusiness.com/products/platform-pricing/">Meta · WhatsApp Business Platform Pricing</a>. Revalidar tabela e condições antes de fechar orçamento comercial.</div>
<p class="tiny"><b>Limites:</b> sem consulta a produção, banco remoto, variáveis de ambiente ou faturas; sem execução paga de modelos. Não foram medidos ativação, conversão, tokens médios ou disponibilidade efetiva por clínica. O padrão de configuração não comprova o modelo efetivo de produção. Não houve commit, push ou deploy. APIs de brain-api e PreCheck não foram inventariadas; o apêndice cobre apenas a SecretarIA.</p>
''')

for offset in range(0, len(ROUTES), 22):
    subset = ROUTES[offset:offset+22]
    rows = ''.join(f'<tr><td>{r["method"]}</td><td><code>{E(r["path"])}</code></td><td><code>{E(r["module"].removeprefix("api."))}</code></td></tr>' for r in subset)
    page(f'api-{offset//22+1}', f'Apêndice / Inventário {offset//22+1:02d}', 'Rotas da SecretarIA.', f'{len(ROUTES)} operações HTTP explícitas registradas em main.py. Nesta página: {offset+1} a {offset+len(subset)}.', f'''
<p class="tiny">Método + caminho efetivo + módulo responsável. Inventário estático do código local, sem sondagem de serviços. Corpo, resposta e dependências de autenticação devem ser consultados nos schemas e na OpenAPI do build correspondente.</p>
<table class="route-table"><colgroup><col style="width:12%"><col style="width:59%"><col style="width:29%"></colgroup><thead><tr><th>Método</th><th>Rota</th><th>Módulo em api/</th></tr></thead><tbody>{rows}</tbody></table>
<div class="source">Extração AST: módulos importados por main.py + include_router + prefixo do APIRouter + decorators HTTP. Fonte por função/linha disponível no arquivo inventario-apis.json. Não inclui documentação automática do FastAPI, rotas de outros produtos ou contratos não registrados.</div>
''')

JS = r'''
const ids=['bookings','activation','conversion','calls','inputTokens','outputTokens','fx','reserve','fixed'];
const defaults=[1000,35,60,3,8000,1000,5.5,30,0];
const brl=(v,d=2)=>v.toLocaleString('pt-BR',{style:'currency',currency:'BRL',minimumFractionDigits:d,maximumFractionDigits:d});
const integer=v=>Math.round(v).toLocaleString('pt-BR');
function recalculate(){
 const fields=ids.map(id=>document.getElementById(id));
 if(fields.some(el=>el.value===''||!el.checkValidity())){
  document.getElementById('unitCost').textContent='Rever campos';
  document.getElementById('monthlyCost').textContent='—';
  document.getElementById('funnel').textContent='Informe valores válidos; conversão precisa ser maior que zero.';
  document.getElementById('allocated').textContent='Resultado indisponível enquanto houver campos inválidos.';
  return;
 }
 const [n,pct,qct,k,i,o,fx,res,fixed]=fields.map(el=>Number(el.value));
 const p=pct/100,q=qct/100,high=i>100000;
 const c=fx*(i*(high?.5:.1)+o*(high?2.5:.5))/1e6;
 const unit=p/q*k*c, monthly=unit*n*(1+res/100), conversations=n/q;
 document.getElementById('unitCost').textContent=brl(unit,5);
 document.getElementById('monthlyCost').textContent=brl(monthly);
 document.getElementById('monthlyNote').textContent=integer(n)+' consultas marcadas.';
 document.getElementById('funnel').textContent=integer(conversations)+' conversas iniciadas → '+integer(conversations*p)+' com IA → '+integer(conversations*p*k)+' requisições → '+integer(n)+' consultas.';
 document.getElementById('allocated').textContent='LLM com reserva + fixos informados: '+brl((monthly+fixed)/n,5)+' por consulta. Outros custos variáveis não incluídos.';
 document.getElementById('tierNote').textContent=(high?'Faixa maior: prompts acima de 100.000 tokens.':'Faixa usada: prompts até 100.000 tokens.')+' Sem desconto de cache; sem tributos e spread.';
}
function resetSimulation(){ids.forEach((id,j)=>document.getElementById(id).value=defaults[j]);recalculate();}
ids.forEach(id=>document.getElementById(id).addEventListener('input',recalculate));recalculate();
'''
doc = f'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="Dossiê SecretarIA: arquitetura, APIs e estimativa de custo por consulta com Haiku 5.5."><title>SecretarIA · Da conversa à consulta</title><style>{CSS}</style></head><body><nav><b>SecretarIA / Dossiê</b><div><a href="#inicio">Início</a><a href="#arquitetura">Arquitetura</a><a href="#cenarios">Custos</a><a href="#simulador">Simulador</a><a href="#api-1">APIs</a></div><button onclick="window.print()">Imprimir / PDF</button></nav>{''.join(PAGES)}<script>{JS}</script></body></html>'''
(OUT / 'SecretarIA_Dossie.html').write_text(doc, encoding='utf-8')

# Memória de cálculo também legível sem navegador.
def cost(p,q,k,i,o):
    return p/q*k*5.5*(i*.1+o*.5)/1_000_000
scenarios = [('Leve',.15,.75,2,4000,500),('Central',.35,.60,3,8000,1000),('Intensivo',.65,.40,6,16000,2000)]
md = f'''# SecretarIA — memória de cálculo e fontes

Data: 28/10/2026. Base: main@{SHA}. Documento principal: SecretarIA_Dossie.html e SecretarIA_Dossie.pdf.

## Como ler

O HTML é autocontido e inclui simulador offline. O PDF registra as premissas centrais e o inventário de APIs.
A análise descreve o código local; não comprova versão implantada ou configurações reais. Nenhum segredo ou dado de paciente foi consultado.

## Modelo econômico

Haiku 5.5, API padrão, faixa até 100.000 tokens de prompt por requisição: US$ 0,10/M de entrada e US$ 0,50/M de saída.
Acima do limiar: US$ 0,50/M e US$ 2,50/M. Cache de 5 min: gravação US$ 0,125/M; leitura US$ 0,01/M na faixa menor.
Tarifas verificadas em 28/10/2026. Câmbio hipotético: R$ 5,50/US$, sem tributos/spread. Sem desconto de cache nos cenários.

`LLM por consulta = (p/q) * k * câmbio * (I * preço_in + O * preço_out) / 1.000.000`

p = conversas com IA / conversas iniciadas; q = consultas novas / conversas iniciadas; k = requisições de modelo por conversa com IA, acumulando todos os turnos.
I/O = tokens médios por requisição, incluindo prompt, histórico, ferramentas, resultados e saída faturável/thinking quando aplicável.
Modelo simplificado: até uma nova consulta por conversa. Custos de conversas sem consulta são atribuídos às consultas concluídas na coorte.

| Cenário | p | q | k | I | O | R$/consulta | R$/1.000 consultas |
|---|---:|---:|---:|---:|---:|---:|---:|
'''
for name,p,q,k,i,o in scenarios:
    val=cost(p,q,k,i,o)
    md += f'| {name} | {p:.0%} | {q:.0%} | {k} | {i} | {o} | {val:.7f} | {val*1000:.4f} |\n'
md += '''
Central exato: R$ 0,0125125/consulta. Com reserva hipotética de 30%: R$ 0,01626625.
Em 1.000 consultas: R$ 12,5125 ou R$ 16,26625 com reserva. Em 10.000: R$ 125,125 ou R$ 162,6625 com reserva.
Um paciente que use apenas o fluxo guiado pode consumir zero tokens de chat; isso não zera custo de canal ou infraestrutura.
Uma conversa com IA central custa R$ 0,02145 antes de atribuir abandonos/conversão. Uma consulta exclusivamente guiada custa R$ 0 de LLM conversacional.

## Não incluído na estimativa LLM

Hospedagem API e worker, Postgres, Redis, observabilidade, suporte, Meta/mensageria, e-mail, áudio OpenAI/Groq, R2, tarifas de pagamento, contexto administrativo, PreCheck e tributos.
Ausência de valor contratual não significa custo zero. A reserva de 30% não é garantia de teto.

## O que medir para conhecer a média

Conversas iniciadas; taxa de ativação; chamadas em agente/ajuda/extração; input normal, cache criação/leitura, output, erros/retries faturados; novas consultas deduplicadas; comparecimento.
Conciliar o uso de todas as rotas com a conta do fornecedor. Evitar contar thinking duas vezes se já incluído em output.
`llm_turn_trace` é um ponto de partida, não um livro contábil completo: saídas por ferramentas, erros e chamadas fora do agente precisam de cobertura.

## Referências externas

- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/models/haiku-5-5/overview
- https://whatsappbusiness.com/products/platform-pricing/

## Fontes locais e escopo

'''
for title,paths,purpose in local_refs:
    md+=f'- **{title}**: {paths}. {purpose}\n'
md+=f'''
Inventário: {len(ROUTES)} operações HTTP explícitas dos routers registrados em main.py; exclui /docs, /redoc e /openapi.json automáticos. Fonte de cada operação em inventario-apis.json.
Graphify STALE (grafo de commit anterior), usado somente para navegação histórica, fatos confirmados no código.
Os checkpoints têm registros históricos de deploy que podem estar defasados. O modelo no código é Haiku 5.5; runtime não inspecionado.
Git preexistente preservado: CLAUDE.md modificado e AGENTS.md não rastreado. Este gerador só cria arquivos nesta pasta de documentação. Sem commit, push, deploy ou alteração de produção.
'''
(OUT / 'Memoria_de_calculo_e_fontes.md').write_text(md, encoding='utf-8')

if __name__ == '__main__':
    print(json.dumps({'html': str(OUT / 'SecretarIA_Dossie.html'), 'pages': len(PAGES), 'routes': len(ROUTES), 'commit': SHA}, ensure_ascii=False))
